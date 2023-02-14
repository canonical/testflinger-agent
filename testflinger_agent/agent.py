# Copyright (C) 2017-2020 Canonical
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

import json
import logging
import multiprocessing
import os
import shutil
import time
import requests
from requests.adapters import HTTPAdapter, Retry
from urllib.parse import urljoin

from testflinger_agent.job import TestflingerJob
from testflinger_agent.errors import TFServerError

logger = logging.getLogger(__name__)


class TestflingerAgent:
    def __init__(self, client):
        self.client = client
        self.agent = self.client.config.get("agent_id")
        server = self.client.config.get("server_address")
        if not server.lower().startswith("http"):
            server = "http://" + server
        data_uri = urljoin(server, "/v1/agents/data/")
        self.data_url = urljoin(data_uri, self.agent)
        self.session = self._requests_retry()
        self._state = multiprocessing.Array("c", 16)
        self.set_state("waiting")
        self.advertised_queues = self.client.config.get("advertised_queues")
        self.advertised_images = self.client.config.get("advertised_images")
        location = self.client.config.get("location")
        if location:
            self._post_json({"location": location})
        if self.advertised_queues:
            self._post_json({"queues": self.advertised_queues})
        if self.advertised_queues or self.advertised_images:
            self.status_proc = multiprocessing.Process(
                target=self._status_worker
            )
            self.status_proc.daemon = True
            self.status_proc.start()

    def _status_worker(self):
        # Report advertised queues to testflinger server when we are listening
        while True:
            # Post every 2min unless the agent is offline
            if self._state.value.decode("utf-8") != "offline":
                if self.advertised_queues:
                    self.client.post_queues(self.advertised_queues)
                if self.advertised_images:
                    self.client.post_images(self.advertised_images)
            time.sleep(120)

    def _requests_retry(self, retries=3):
        session = requests.Session()
        retry = Retry(
            total=retries,
            read=retries,
            connect=retries,
            backoff_factor=0.3,
            status_forcelist=(500, 502, 503, 504),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _post_json(self, data):
        try:
            self.session.post(
                url=self.data_url,
                data=json.dumps(data),
                headers={"Content-type": "application/json"},
            )
        except Exception as e:
            logger.exception(e)

    def set_state(self, state):
        self._post_json({"state": state})
        self._state.value = state.encode("utf-8")

    def get_offline_files(self):
        # Return possible restart filenames with and without dashes
        # i.e. support both:
        #     TESTFLINGER-DEVICE-OFFLINE-devname-001
        #     TESTFLINGER-DEVICE-OFFLINE-devname001
        files = [
            "/tmp/TESTFLINGER-DEVICE-OFFLINE-{}".format(self.agent),
            "/tmp/TESTFLINGER-DEVICE-OFFLINE-{}".format(
                self.agent.replace("-", "")
            ),
        ]
        return files

    def get_restart_files(self):
        # Return possible restart filenames with and without dashes
        # i.e. support both:
        #     TESTFLINGER-DEVICE-RESTART-devname-001
        #     TESTFLINGER-DEVICE-RESTART-devname001
        files = [
            "/tmp/TESTFLINGER-DEVICE-RESTART-{}".format(self.agent),
            "/tmp/TESTFLINGER-DEVICE-RESTART-{}".format(
                self.agent.replace("-", "")
            ),
        ]
        return files

    def check_offline(self):
        possible_files = self.get_offline_files()
        for offline_file in possible_files:
            if os.path.exists(offline_file):
                self.set_state("offline")
                return offline_file
        self.set_state("waiting")
        return ""

    def check_restart(self):
        possible_files = self.get_restart_files()
        for restart_file in possible_files:
            if os.path.exists(restart_file):
                try:
                    os.unlink(restart_file)
                    logger.info("Restarting agent")
                    self.set_state("offline")
                    raise SystemExit("Restart Requested")
                except OSError:
                    logger.error(
                        "Restart requested, but unable to remove marker file!"
                    )
                    break

    def check_job_state(self, job_id):
        job_data = self.client.get_result(job_id)
        if job_data:
            return job_data.get("job_state")

    def mark_device_offline(self):
        # Create the offline file, this should work even if it exists
        open(self.get_offline_files()[0], "w").close()

    def process_jobs(self):
        """Coordinate checking for new jobs and handling them if they exists"""
        TEST_PHASES = ["setup", "provision", "test", "reserve"]

        # First, see if we have any old results that we couldn't send last time
        self.retry_old_results()

        self.check_restart()

        job_data = self.client.check_jobs()
        while job_data:
            try:
                job = TestflingerJob(job_data, self.client)
                logger.info("Starting job %s", job.job_id)
                self._post_json({"job_id": job.job_id})
                rundir = os.path.join(
                    self.client.config.get("execution_basedir"), job.job_id
                )
                os.makedirs(rundir)
                # Dump the job data to testflinger.json in our execution dir
                with open(os.path.join(rundir, "testflinger.json"), "w") as f:
                    json.dump(job_data, f)
                # Create json outcome file where phases will store their output
                with open(
                    os.path.join(rundir, "testflinger-outcome.json"), "w"
                ) as f:
                    json.dump({}, f)

                for phase in TEST_PHASES:
                    # First make sure the job hasn't been cancelled
                    if self.check_job_state(job.job_id) == "cancelled":
                        logger.info("Job cancellation was requested, exiting.")
                        break
                    # Try to update the job_state on the testflinger server
                    try:
                        self.client.post_result(
                            job.job_id, {"job_state": phase}
                        )
                    except TFServerError:
                        pass
                    self.set_state(phase)
                    proc = multiprocessing.Process(
                        target=job.run_test_phase,
                        args=(
                            phase,
                            rundir,
                        ),
                    )
                    proc.start()
                    while proc.is_alive():
                        proc.join(10)
                        if (
                            self.check_job_state(job.job_id) == "cancelled"
                            and phase != "provision"
                        ):
                            logger.info(
                                "Job cancellation was requested, exiting."
                            )
                            proc.terminate()
                    exitcode = proc.exitcode

                    # exit code 46 is our indication that recovery failed!
                    # In this case, we need to mark the device offline
                    if exitcode == 46:
                        self.mark_device_offline()
                        self.client.repost_job(job_data)
                        shutil.rmtree(rundir)
                        # Return NOW so we don't keep trying to process jobs
                        return
                    if phase != "test" and exitcode:
                        logger.debug("Phase %s failed, aborting job" % phase)
                        break
            except Exception as e:
                logger.exception(e)
            finally:
                # Always run the cleanup, even if the job was cancelled
                proc = multiprocessing.Process(
                    target=job.run_test_phase,
                    args=(
                        "cleanup",
                        rundir,
                    ),
                )
                proc.start()
                proc.join()

            try:
                self.client.transmit_job_outcome(rundir)
            except Exception as e:
                # TFServerError will happen if we get other-than-good status
                # Other errors can happen too for things like connection
                # problems
                logger.exception(e)
                results_basedir = self.client.config.get("results_basedir")
                shutil.move(rundir, results_basedir)
            self.set_state("waiting")

            self.check_restart()
            if self.check_offline():
                # Don't get a new job if we are now marked offline
                break
            job_data = self.client.check_jobs()

    def retry_old_results(self):
        """Retry sending results that we previously failed to send"""

        results_dir = self.client.config.get("results_basedir")
        # List all the directories in 'results_basedir', where we store the
        # results that we couldn't transmit before
        old_results = [
            os.path.join(results_dir, d)
            for d in os.listdir(results_dir)
            if os.path.isdir(os.path.join(results_dir, d))
        ]
        for result in old_results:
            try:
                logger.info("Attempting to send result: %s" % result)
                self.client.transmit_job_outcome(result)
            except TFServerError:
                # Problems still, better luck next time?
                pass