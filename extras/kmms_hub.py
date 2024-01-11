# KMMS hub config
#
# Copyright (C) 2023-2024  Michal Dvorak <mikee2185@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


class KmmsHub(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name().split()[-1]

        filament_available_pins = config.getlist('filament_available_pins')
        self.filament_available_names = list(self._filament_available_switch_name(i)
                                             for i in range(len(filament_available_pins)))
        self.filament_available_switches = list(
            self._define_filament_switch(config, name, pin) for name, pin in
            zip(self.filament_available_names, filament_available_pins))

        self.filament_switch = self._define_filament_switch(config, self.name, config.get('filament_switch_pin'))

        # Commands
        self.gcode.register_mux_command("__KMMS_HUB_INSERT", "HUB", self.name,
                                        self.cmd__KMMS_HUB_INSERT)
        self.gcode.register_mux_command("__KMMS_HUB_RUNOUT", "HUB", self.name,
                                        self.cmd__KMMS_HUB_RUNOUT)

    def _handle_insert(self, eventtime, name):
        if name == self.name:
            self.gcode.respond_info("Filament detected at %s" % name)
            self.printer.send_event('kmms:hub_insert', eventtime, self.name)
        elif name in self.filament_available_names:
            self.gcode.respond_info("Filament now available at %s" % name)
            self.printer.send_event('kmms:hub_available', eventtime, self.name, name.split('_')[-1])

    def _handle_runout(self, eventtime, name):
        if name == self.name:
            self.gcode.respond_info("Filament removed from %s" % name)
            self.printer.send_event('kmms:hub_runout', eventtime, self.name)
        elif name in self.filament_available_names:
            self.gcode.respond_info("Filament no longer available at %s" % name)
            self.printer.send_event('kmms:hub_unavailable', eventtime, self.name, name.split('_')[-1])

    def get_status(self, eventtime):
        return {
            'filament_detected': self.filament_switch.get_status(eventtime)['filament_detected'],
            'filament_available': list(
                fs.get_status(eventtime)['filament_detected'] for fs in self.filament_available_switches),
        }

    def _filament_available_switch_name(self, index):
        return "%s_%d" % (self.name, index)

    def _define_filament_switch(self, config, name, switch_pin):
        section = "filament_switch_sensor %s" % name

        config.fileconfig.add_section(section)
        config.fileconfig.set(section, "switch_pin", switch_pin)
        config.fileconfig.set(section, "pause_on_runout", "False")
        config.fileconfig.set(section, "event_delay", 0.1)
        config.fileconfig.set(section, "run_always", "True")
        config.fileconfig.set(section, "insert_gcode", "__KMMS_HUB_INSERT HUB=%s NAME=%s" % (self.name, name))
        config.fileconfig.set(section, "runout_gcode", "__KMMS_HUB_RUNOUT HUB=%s NAME=%s" % (self.name, name))

        return self.printer.load_object(config, section)

    def cmd__KMMS_HUB_INSERT(self, gcmd):
        name = gcmd.get('NAME')
        self.reactor.register_callback(lambda eventtime: self._handle_insert(eventtime, name))

    def cmd__KMMS_HUB_RUNOUT(self, gcmd):
        name = gcmd.get('NAME')
        self.reactor.register_callback(lambda eventtime: self._handle_runout(eventtime, name))


def load_config_prefix(config):
    return KmmsHub(config)
