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

        available_switch_pins = config.getlist('available_switch_pins')
        self.available_switch_pin_names = [self._available_switch_name(i)
                                           for i in range(len(available_switch_pins))]
        self.available_switch_list = [self._define_filament_switch_sensor(config, name, pin) for name, pin in
                                      zip(self.available_switch_pin_names, available_switch_pins, strict=True)]

        self.filament_switch = self._define_filament_switch_sensor(config, self.name, config.get('filament_switch_pin'))

        # Commands
        self.gcode.register_mux_command("__KMMS_HUB_INSERT", "HUB", self.name,
                                        self.cmd__KMMS_HUB_INSERT)
        self.gcode.register_mux_command("__KMMS_HUB_RUNOUT", "HUB", self.name,
                                        self.cmd__KMMS_HUB_RUNOUT)

    def _handle_insert(self, eventtime, name):
        if name == self.name:
            self.gcode.respond_info("Filament detected at %s" % name)
            # self.printer.send_event('kmms:hub_insert', eventtime, self.name)
        elif name in self.available_switch_pin_names:
            self.gcode.respond_info("Filament now available at %s" % name)
            # self.printer.send_event('kmms:hub_available', eventtime, self.name, name.split('_')[-1])

    def _handle_runout(self, eventtime, name):
        if name == self.name:
            self.gcode.respond_info("Filament removed from %s" % name)
            # self.printer.send_event('kmms:hub_runout', eventtime, self.name)
        elif name in self.available_switch_pin_names:
            self.gcode.respond_info("Filament no longer available at %s" % name)
            # self.printer.send_event('kmms:hub_unavailable', eventtime, self.name, name.split('_')[-1])

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
        config.fileconfig.set(section, "run_always", 1)
        config.fileconfig.set(section, "event_delay", 0.1)
        config.fileconfig.set(section, "insert_gcode", "__KMMS_HUB_INSERT HUB=%s NAME=%s", self.name, name)
        config.fileconfig.set(section, "runout_gcode", "__KMMS_HUB_RUNOUT HUB=%s NAME=%s", self.name, name)

        return self.printer.load_object(config, section)

    def cmd__KMMS_HUB_INSERT(self, gcmd):
        name = gcmd.get('NAME')
        self.reactor.register_callback(lambda eventtime: self._handle_insert(eventtime, name))

    def cmd__KMMS_HUB_RUNOUT(self, gcmd):
        name = gcmd.get('NAME')
        self.reactor.register_callback(lambda eventtime: self._handle_runout(eventtime, name))


def load_config_prefix(config):
    return KmmsHub(config)
