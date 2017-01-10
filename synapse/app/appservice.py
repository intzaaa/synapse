#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import synapse

from synapse.server import HomeServer
from synapse.config._base import ConfigError
from synapse.config.logger import setup_logging
from synapse.config.homeserver import HomeServerConfig
from synapse.http.site import SynapseSite
from synapse.metrics.resource import MetricsResource, METRICS_PREFIX
from synapse.replication.slave.storage.directory import DirectoryStore
from synapse.replication.slave.storage.events import SlavedEventStore
from synapse.replication.slave.storage.appservice import SlavedApplicationServiceStore
from synapse.replication.slave.storage.registration import SlavedRegistrationStore
from synapse.storage.engines import create_engine
from synapse.util.async import sleep
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext
from synapse.util.manhole import manhole
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string

from synapse import events

from twisted.internet import reactor, defer
from twisted.web.resource import Resource

from daemonize import Daemonize

import sys
import logging
import gc

logger = logging.getLogger("synapse.app.appservice")


class AppserviceSlaveStore(
    DirectoryStore, SlavedEventStore, SlavedApplicationServiceStore,
    SlavedRegistrationStore,
):
    pass


class AppserviceServer(HomeServer):
    def get_db_conn(self, run_new_connection=True):
        # Any param beginning with cp_ is a parameter for adbapi, and should
        # not be passed to the database engine.
        db_params = {
            k: v for k, v in self.db_config.get("args", {}).items()
            if not k.startswith("cp_")
        }
        db_conn = self.database_engine.module.connect(**db_params)

        if run_new_connection:
            self.database_engine.on_new_connection(db_conn)
        return db_conn

    def setup(self):
        logger.info("Setting up.")
        self.datastore = AppserviceSlaveStore(self.get_db_conn(), self)
        logger.info("Finished setting up.")

    def _listen_http(self, listener_config):
        port = listener_config["port"]
        bind_address = listener_config.get("bind_address", None)
        bind_addresses = listener_config.get("bind_addresses", [])
        site_tag = listener_config.get("tag", port)
        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(self)

        root_resource = create_resource_tree(resources, Resource())

        if bind_address is not None:
            bind_addresses.append(bind_address)

        for address in bind_addresses:
            reactor.listenTCP(
                port,
                SynapseSite(
                    "synapse.access.http.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                ),
                interface=address
            )

        logger.info("Synapse appservice now listening on port %d", port)

    def start_listening(self, listeners):
        for listener in listeners:
            if listener["type"] == "http":
                self._listen_http(listener)
            elif listener["type"] == "manhole":
                bind_address = listener.get("bind_address", None)
                bind_addresses = listener.get("bind_addresses", [])

                if bind_address is not None:
                    bind_addresses.append(bind_address)

                for address in bind_addresses:
                    reactor.listenTCP(
                        listener["port"],
                        manhole(
                            username="matrix",
                            password="rabbithole",
                            globals={"hs": self},
                        ),
                        interface=address
                    )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

    @defer.inlineCallbacks
    def replicate(self):
        http_client = self.get_simple_http_client()
        store = self.get_datastore()
        replication_url = self.config.worker_replication_url
        appservice_handler = self.get_application_service_handler()

        @defer.inlineCallbacks
        def replicate(results):
            stream = results.get("events")
            if stream:
                max_stream_id = stream["position"]
                yield appservice_handler.notify_interested_services(max_stream_id)

        while True:
            try:
                args = store.stream_positions()
                args["timeout"] = 30000
                result = yield http_client.get_json(replication_url, args=args)
                yield store.process_replication(result)
                replicate(result)
            except:
                logger.exception("Error replicating from %r", replication_url)
                yield sleep(30)


def start(config_options):
    try:
        config = HomeServerConfig.load_config(
            "Synapse appservice", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    assert config.worker_app == "synapse.app.appservice"

    setup_logging(config.worker_log_config, config.worker_log_file)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    database_engine = create_engine(config.database_config)

    if config.notify_appservices:
        sys.stderr.write(
            "\nThe appservices must be disabled in the main synapse process"
            "\nbefore they can be run in a separate worker."
            "\nPlease add ``notify_appservices: false`` to the main config"
            "\n"
        )
        sys.exit(1)

    # Force the pushers to start since they will be disabled in the main config
    config.notify_appservices = True

    ps = AppserviceServer(
        config.server_name,
        db_config=config.database_config,
        config=config,
        version_string="Synapse/" + get_version_string(synapse),
        database_engine=database_engine,
    )

    ps.setup()
    ps.start_listening(config.worker_listeners)

    def run():
        with LoggingContext("run"):
            logger.info("Running")
            change_resource_limit(config.soft_file_limit)
            if config.gc_thresholds:
                gc.set_threshold(*config.gc_thresholds)
            reactor.run()

    def start():
        ps.replicate()
        ps.get_datastore().start_profiling()
        ps.get_state_handler().start_caching()

    reactor.callWhenRunning(start)

    if config.worker_daemonize:
        daemon = Daemonize(
            app="synapse-appservice",
            pid=config.worker_pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )
        daemon.start()
    else:
        run()


if __name__ == '__main__':
    with LoggingContext("main"):
        start(sys.argv[1:])
