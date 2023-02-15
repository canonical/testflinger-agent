# Copyright (C) 2016-2023 Canonical
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

import argparse
import logging
import os
import time
import yaml
import json
import requests
from requests.adapters import HTTPAdapter, Retry
from urllib.parse import urljoin
from threading import Thread, Timer

from testflinger_agent import schema
from testflinger_agent.agent import TestflingerAgent
from testflinger_agent.client import TestflingerClient
from logging.handlers import TimedRotatingFileHandler

logger = logging.getLogger(__name__)


class LoopTimer(Timer):
    """Loop thread timer (interval)"""

    def run(self):
        while not self.finished.wait(self.interval):
            self.function(*self.args, **self.kwargs)


class ReqLogger(Thread):
    """Threaded requests logger"""

    def __init__(self, agent, server):
        super().__init__()
        self.daemon = True
        self.agent = agent
        self.server = server
        self.request_handler = None
        self.config_reqlogger()

    def config_reqlogger(self):
        """configure logging"""
        # inherit from logger __name__
        req_logger = logging.getLogger()
        # inherit level
        # req_logger.setLevel(logging.info)
        request_formatter = ReqFormatter()
        self.request_handler = ReqBufferHandler(self.agent, self.server)
        self.request_handler.setFormatter(request_formatter)
        req_logger.addHandler(self.request_handler)

    def join(self):
        """cleanup children"""
        self.request_handler.reqbuf_timer.stop()
        self.request_handler.close()


class ReqBufferHandler(logging.Handler):
    """Requests logging handler"""

    def __init__(self, agent, server):
        super().__init__()
        self.server = server
        uri = urljoin(server, "/v1/agents/data/")
        self.url = urljoin(uri, agent)
        self.buffer = []
        self.qdepth = 100  # messages
        self.reqbuf_timer = None
        self.reqbuf_interval = 10.0  # seconds
        self._start_rb_timer()
        # reuse socket
        self.session = self._requests_retry()

    def _requests_retry(self, retries=3):
        """retry api server"""
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

    def _start_rb_timer(self):
        """periodically check and send buffer"""
        self.reqbuf_timer = LoopTimer(self.reqbuf_interval, self.flush)
        self.reqbuf_timer.daemon = True
        self.reqbuf_timer.start()

    def emit(self, record):
        """write logging events to buffer"""
        if len(self.buffer) >= self.qdepth:
            self.flush(bfull=True)

        self.buffer.append(record)

    def flush(self, bfull=False):
        """flush and post buffer"""
        try:
            for record in self.buffer:
                self.session.post(
                    url=self.url,
                    data=self.format(record),
                    headers={"Content-type": "application/json"},
                    timeout=3,
                )
        except Exception as e:
            logger.exception(e)

            # preserve buffer
            if not bfull:
                pass

        self.buffer = []


class ReqFormatter(logging.Formatter):
    """Format logging messages"""

    def format(self, record):
        data = {"log": [record.getMessage()]}

        return json.dumps(data)


def start_agent():
    args = parse_args()
    config = load_config(args.config)
    # requests logging
    server = config.get("server_address")
    if not server.lower().startswith("http"):
        server = "http://" + server
    reqlog_thread = Thread(
        target=ReqLogger, args=(config.get("agent_id"), server)
    )
    reqlog_thread.start()
    # general logging
    configure_logging(config)
    check_interval = config.get("polling_interval")
    client = TestflingerClient(config)
    agent = TestflingerAgent(client)
    while True:
        offline_file = agent.check_offline()
        if offline_file:
            logger.error(
                "Agent %s is offline, not processing jobs! "
                "Remove %s to resume processing"
                % (config.get("agent_id"), offline_file)
            )
            while agent.check_offline():
                time.sleep(check_interval)
        logger.info("Checking jobs")
        agent.process_jobs()
        logger.info("Sleeping for {}".format(check_interval))
        time.sleep(check_interval)

    reqlog_thread.join()


def load_config(configfile):
    with open(configfile) as f:
        config = yaml.safe_load(f)
    config = schema.validate(config)
    return config


def configure_logging(config):
    # Create these at the beginning so we fail early if there are
    # permission problems
    os.makedirs(config.get("logging_basedir"), exist_ok=True)
    os.makedirs(config.get("results_basedir"), exist_ok=True)
    log_level = logging.getLevelName(config.get("logging_level"))
    # This should help if they specify something invalid
    if not isinstance(log_level, int):
        log_level = logging.INFO
    logfmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)+7.7s: %(message)s",
        datefmt="%y-%m-%d %H:%M:%S",
    )
    log_path = os.path.join(
        config.get("logging_basedir"), "testflinger-agent.log"
    )
    file_log = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=6
    )
    file_log.setFormatter(logfmt)
    logger.addHandler(file_log)
    if not config.get("logging_quiet"):
        console_log = logging.StreamHandler()
        console_log.setFormatter(logfmt)
        logger.addHandler(console_log)
    logger.setLevel(log_level)


def parse_args():
    parser = argparse.ArgumentParser(description="Testflinger Agent")
    parser.add_argument(
        "--config",
        "-c",
        default="testflinger-agent.conf",
        help="Testflinger agent config file",
    )
    return parser.parse_args()