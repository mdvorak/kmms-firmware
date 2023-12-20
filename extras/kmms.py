# KMMS hub config
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class Kmms:
    def __init__(self, config):
        self.printer = config.get_printer()

        self.spools = []
        self.hubs = []

        self.status = -1
        self.selected_spool = None

        self.printer.register_event_handler("klippy:connect", self._handle_connect)

    def _handle_connect(self):
        self.spools = self.printer.lookup_objects(module="kmms_spool")
        self.hubs = self.printer.lookup_objects(module="kmms_hub")


def load_config(config):
    return Kmms(config)
