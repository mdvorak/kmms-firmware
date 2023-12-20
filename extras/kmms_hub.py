# KMMS hub config
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class KmmsHub(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]

        filament_sensor_pins = config.getlist('filament_available_sensor_pins')
        self.filament_available_sensor_names = list(self._filament_available_switch_name(i)
                                                    for i in range(len(filament_sensor_pins)))
        self.filament_available_sensor_switches = list(
            self._define_filament_switch(config, name, pin) for name, pin in
            zip(self.filament_available_sensor_names, filament_sensor_pins))

        self.filament_sensor_switch = self._define_filament_switch(config, self.name, config.get('filament_sensor_pin'))

        # Events
        self.printer.register_event_handler("filament:insert", self._handle_insert)
        self.printer.register_event_handler("filament:runout", self._handle_runout)

    def _handle_insert(self, eventtime, name):
        if name == self.filament_sensor_switch.name:
            self.printer.send_event('kmms:hub_filament_insert', eventtime, self.name)
        elif name in self.filament_available_sensor_names:
            self.printer.send_event('kmms:hub_filament_available', eventtime, self.name, name.split('_')[-1])

    def _handle_runout(self, eventtime, name):
        if name == self.filament_sensor_switch.name:
            self.printer.send_event('kmms:hub_filament_runout', eventtime, self.name)
        elif name in self.filament_available_sensor_names:
            self.printer.send_event('kmms:hub_filament_unavailable', eventtime, self.name, name.split('_')[-1])

    def get_status(self, eventtime):
        return {
            'filament_present': self.filament_sensor_switch.get_status(eventtime)['filament_present'],
            'filament_available': list(
                fs.get_status(eventtime)['filament_present'] for fs in self.filament_available_sensor_switches),
        }

    def _filament_available_switch_name(self, index):
        return "%s_%d" % (self.name, index)

    def _define_filament_switch(self, config, name, switch_pin):
        section = "filament_switch_sensor %s" % name

        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", "False")
        config.fileconfig.set(section, "event_delay", 1.)

        return self.printer.load_object(config, section)


def load_config_prefix(config):
    return KmmsHub(config)
