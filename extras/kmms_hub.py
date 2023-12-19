# KMMS hub config
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


class KmmsHub(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]

        filament_sensor_pins = config.getlist('filament_sensor_pins')
        self.filament_sensor_switches = []  # TODO

        self.entry_sensor_switch = self._define_filament_switch(config, self.name, config.get('entry_sensor_pin'),
                                                                '__KMMS_HUB_ENTRY')

    def _define_filament_switch(self, config, name, switch_pin, gcode_handler):
        section = "filament_switch_sensor %s" % name
        insert_gcode = "%s_INSERT SPOOL=%s" % (gcode_handler, self.name)
        runout_gcode = "%s_RUNOUT SPOOL=%s" % (gcode_handler, self.name)

        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", "False")
        config.fileconfig.set(section, "insert_gcode", insert_gcode)
        config.fileconfig.set(section, "runout_gcode", runout_gcode)

        return self.printer.load_object(config, section)


def load_config_prefix(config):
    return KmmsHub(config)
