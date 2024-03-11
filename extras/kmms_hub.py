# KMMS
#
# Copyright (C) 2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging


class Hub(object):
    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]

        # Read config
        self.filament_switch = self._define_filament_switch_sensor(config, self.name, config.get('filament_switch_pin'))

        available_switch_pins = config.getlist('available_switch_pins')
        self.available_switch_pin_names = [self._available_switch_name(i)
                                           for i in range(len(available_switch_pins))]
        self.available_switch_list = [self._define_filament_switch_sensor(config, name, pin) for name, pin in
                                      zip(self.available_switch_pin_names, available_switch_pins, strict=True)]

    def get_filament_detected(self):
        return self.filament_switch.runout_helper.filament_present

    def get_filament_available(self):
        return [fs.runout_helper.filament_present for fs in self.available_switch_list]

    def get_status(self, eventtime):
        return {
            'filament_detected': self.get_filament_detected(),
            'filament_available': self.get_filament_available(),
        }

    def _available_switch_name(self, index):
        return "%s_available_%d" % (self.name, index)

    def _define_filament_switch_sensor(self, config, name, switch_pin):
        section = "kmms_filament_switch_sensor %s" % name

        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", 0)
        config.fileconfig.set(section, "event_delay", 0.1)

        return self.printer.load_object(config, section)


def load_config_prefix(config):
    return Hub(config)
