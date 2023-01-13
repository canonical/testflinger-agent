# Copyright (C) 2017 Canonical
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
import os
import select
import signal
import sys
import subprocess
import time

logger = logging.getLogger(__name__)


class TestflingerJob:
    def __init__(self, job_data, client):
        """
        :param job_data:
            Dictionary containing data for the test job_data
        :param client:
            Testflinger client object for communicating with the server
        """
        self.client = client
        self.job_data = job_data
        self.job_id = job_data.get("job_id")
        self.phase = "unknown"

    def run_test_phase(self, phase, rundir):
        """Run the specified test phase in rundir

        :param phase:
            Name of the test phase (setup, provision, test, ...)
        :param rundir:
            Directory in which to run the command defined for the phase
        :return:
            Returncode from the command that was executed, 0 will be returned
            if there was no command to run
        """
        self.phase = phase
        cmd = self.client.config.get(phase + "_command")
        node = self.client.config.get("agent_id")
        if not cmd:
            logger.info("No %s_command configured, skipping...", phase)
            return 0
        if phase == "provision" and not self.job_data.get("provision_data"):
            logger.info("No provision_data defined in job data, skipping...")
            return 0
        if phase == "test" and not self.job_data.get("test_data"):
            logger.info("No test_data defined in job data, skipping...")
            return 0
        if phase == "reserve" and not self.job_data.get("reserve_data"):
            return 0
        output_log = os.path.join(rundir, phase + ".log")
        serial_log = os.path.join(rundir, phase + "-serial.log")
        logger.info("Running %s_command: %s", phase, cmd)
        # Set the exitcode to some failed status in case we get interrupted
        exitcode = 99

        for line in self.banner(
            "Starting testflinger {} phase on {}".format(phase, node)
        ):
            self.run_with_log("echo '{}'".format(line), output_log, rundir)
        try:
            exitcode = self.run_with_log(cmd, output_log, rundir)
        except Exception as e:
            logger.exception(e)
        finally:
            with open(os.path.join(rundir, "testflinger-outcome.json")) as f:
                outcome_data = json.load(f)
            if os.path.exists(output_log):
                with open(output_log, "r+", encoding="utf-8") as f:
                    self._set_truncate(f)
                    outcome_data[phase + "_output"] = f.read()
            if os.path.exists(serial_log):
                with open(serial_log, "r+", encoding="utf-8") as f:
                    self._set_truncate(f)
                    outcome_data[phase + "_serial"] = f.read()
            outcome_data[phase + "_status"] = exitcode
            with open(
                os.path.join(rundir, "testflinger-outcome.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(outcome_data, f)
            sys.exit(exitcode)

    def _set_truncate(self, f, size=1024 * 1024):
        """Set up an open file so that we don't read more than a specified
           size. We want to read from the end of the file rather than the
           beginning. Write a warning at the end of the file if it was too big.

        :param f:
            The file object, which should be opened for read/write
        :param size:
            Maximum number of bytes we want to allow from reading the file
        """
        end = f.seek(0, 2)
        if end > size:
            f.write("\nWARNING: File has been truncated due to length!")
            f.seek(end - size, 0)
        else:
            f.seek(0, 0)

    def _send_output(self, output, logfile):
        """Send output to the logfile and the server

        :param output:
            String containing the output to send
        :param logfile:
            Name of the logfile to send the output to
        """
        # Write the output to the local logfile
        with open(logfile, "a", encoding="utf-8") as _file:
            _file.write(output)

        # Send the output to the server
        self.client.post_live_output(self.job_id, output)

    def run_with_log(self, cmd, logfile, cwd=None):
        """Run a command and log the output to a file

        :param cmd:
            Command to run
        :param logfile:
            Name of the logfile to send the output to
        :param cwd:
            Directory in which to run the command
        :return:
            Returncode from the command that was executed
        """
        env = os.environ.copy()
        # Make sure there all values we add are strings
        env.update(
            {k: v for k, v in self.client.config.items() if isinstance(v, str)}
        )
        global_timeout = self.get_global_timeout()
        output_timeout = self.get_output_timeout()
        start_time = last_output_time = time.time()
        process = subprocess.Popen(  # pylint: disable=consider-using-with
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            cwd=cwd,
            env=env,
        )
        # Make sure we don't block on the stdout pipe
        os.set_blocking(process.stdout.fileno(), False)

        # Setup select.poll to check for output
        readpoll = select.poll()
        readpoll.register(process.stdout, select.POLLIN)

        def cleanup(*_):
            process.kill()

        signal.signal(signal.SIGTERM, cleanup)

        while True:
            # This is just a fancy sleep(10) that stops early if we get output
            readpoll.poll(10000)
            output = process.stdout.read()
            if output:
                output = output.decode("utf-8", errors="replace")
                # Reset the last_output_time
                last_output_time = time.time()

                # Write the output to the logfile and send it to the server
                self._send_output(output, logfile)

            # Check if we are exiting
            if process.poll() is not None:
                return _get_exit_code(process)

            # Check for output timeout only during the test phase
            if (
                self.phase == "test"
                and time.time() - last_output_time > output_timeout
            ):
                output = (
                    f"\nERROR: Output timeout reached! ({output_timeout}s)\n"
                )
                self._send_output(output, logfile)
                process.kill()
                return _get_exit_code(process)

            # Check for global timeout for any phase except reserve
            if (
                self.phase != "reserve"
                and time.time() - start_time > global_timeout
            ):
                output = (
                    f"\nERROR: Global timeout reached! ({global_timeout}s)\n"
                )
                self._send_output(output, logfile)
                process.kill()
                return _get_exit_code(process)

    def get_global_timeout(self):
        """Get the global timeout for the test run in seconds"""
        # Default timeout is 4 hours
        default_timeout = 4 * 60 * 60

        # Don't exceed the maximum timeout configured for the device!
        return min(
            self.job_data.get("global_timeout", default_timeout),
            self.client.config.get("global_timeout", default_timeout),
        )

    def get_output_timeout(self):
        """Get the output timeout for the test run in seconds"""
        # Default timeout is 15 minutes
        default_timeout = 15 * 60

        # Don't exceed the maximum timeout configured for the device!
        return min(
            self.job_data.get("output_timeout", default_timeout),
            self.client.config.get("output_timeout", default_timeout),
        )

    def banner(self, line):
        """Yield text lines to print a banner around a sting

        :param line:
            Line of text to print a banner around
        """
        yield "*" * (len(line) + 4)
        yield "* {} *".format(line)
        yield "*" * (len(line) + 4)


def _get_exit_code(process):
    try:
        return process.wait()
    except TimeoutError:
        return 99  # Default error in case something goes wrong
