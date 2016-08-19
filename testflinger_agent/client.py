# Copyright (C) 2016 Canonical
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import json
import os
import requests
import subprocess
import sys
import time

from urllib.parse import urljoin

import testflinger_agent

logger = logging.getLogger()


def process_jobs():
    """Coordinate checking for new jobs and handling them if they exists"""
    TEST_PHASES = ['setup', 'provision', 'test']
    job_data = check_jobs()
    while job_data:
        logger.info("Starting job %s", job_data.get('job_id'))
        rundir = os.path.join(
            testflinger_agent.config.get('execution_basedir'),
            job_data.get('job_id'))
        os.makedirs(rundir)
        # Dump the job data to testflinger.json in our execution directory
        with open(os.path.join(rundir, 'testflinger.json'), 'w') as f:
            json.dump(job_data, f)
        # Create json outcome file where phases will store their output
        with open(os.path.join(rundir, 'testflinger-outcome.json'), 'w') as f:
            json.dump({}, f)

        for phase in TEST_PHASES:
            exitcode = run_test_phase(phase, rundir)
            if exitcode:
                logger.debug('Phase %s failed, aborting job' % phase)
                break
        transmit_job_outcome(rundir)

        job_data = check_jobs()


def check_jobs():
    """Check for new jobs for on the Testflinger server

    :return: Dict with job data, or None if no job found
    """
    try:
        server = testflinger_agent.config.get('server_address')
        if not server.lower().startswith('http'):
            server = 'http://' + server
        job_uri = urljoin(server, '/v1/job')
        queue_list = testflinger_agent.config.get('job_queues')
        logger.debug("Requesting a job")
        job_request = requests.get(job_uri, params={'queue': queue_list})
        if job_request.content:
            return job_request.json()
        else:
            return None
    except Exception as e:
        logger.exception(e)
        # Wait a little extra before trying again
        time.sleep(60)


def run_test_phase(phase, rundir):
    """Run the specified test phase in rundir

    :param phase:
        Name of the test phase (setup, provision, test, ...)
    :param rundir:
        Directory in which to run the command defined for the phase
    :return:
        Returncode from the command that was executed, 0 will be returned
        if there was no command to run
    """
    cmd = testflinger_agent.config.get(phase+'_command')
    if not cmd:
        return 0
    phase_log = os.path.join(rundir, phase+'.log')
    logger.info('Running %s_command: %s' % (phase, cmd))
    try:
        exitcode = run_with_log(cmd, phase_log, rundir)
    finally:
        # Save the output log in the json file no matter what
        with open(os.path.join(rundir, 'testflinger-outcome.json')) as f:
            outcome_data = json.load(f)
        with open(phase_log) as f:
            outcome_data[phase+'_output'] = f.read()
            outcome_data[phase+'_status'] = exitcode
        with open(os.path.join(rundir, 'testflinger-outcome.json'), 'w') as f:
            json.dump(outcome_data, f)
        return exitcode


def transmit_job_outcome(rundir):
    """Post job outcome json data to the testflinger server

    :param rundir:
        Execution dir where the results can be found
    """
    server = testflinger_agent.config.get('server_address')
    if not server.lower().startswith('http'):
        server = 'http://' + server
    # Create uri for API: /v1/result/<job_id>
    with open(os.path.join(rundir, 'testflinger.json')) as f:
        job_data = json.load(f)
    job_id = job_data.get('job_id')
    result_uri = urljoin(server, '/v1/result/')
    result_uri = urljoin(result_uri, job_id)
    logger.info('Submitting job outcome for job: %s' % job_id)
    with open(os.path.join(rundir, 'testflinger-outcome.json')) as f:
        job_request = requests.post(result_uri, json=json.load(f))
        if job_request.status_code != 200:
            logging.error('Unable to post results to: %s (error: %s)' %
                          (result_uri, job_request.status_code))


def run_with_log(cmd, logfile, cwd=None):
    """Execute command in a subprocess and log the output

    :param cmd:
        Command to run
    :param logfile:
        Filename to save the output in
    :param cwd:
        Path to run the command from
    :return:
        returncode from the process
    """
    with open(logfile, 'w') as f:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   shell=True, cwd=cwd)
        while process.poll() is None:
            line = process.stdout.readline()
            if line:
                sys.stdout.write(line.decode())
                f.write(line.decode())
                f.flush()
        line = process.stdout.read()
        if line:
            sys.stdout.write(line.decode())
            f.write(line.decode())
        return process.returncode