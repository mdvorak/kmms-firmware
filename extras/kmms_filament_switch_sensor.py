import logging


class KmmsRunoutHelper:
    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.printer.load_object(config, 'pause_resume')

        # Read config
        self.runout_pause = bool(config.getboolean('pause_on_runout', True))
        self.runout_gcode = self.insert_gcode = None
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        if config.get('runout_gcode', None) is not None:
            self.runout_gcode = gcode_macro.load_template(config, 'runout_gcode')
        if config.get('insert_gcode', None) is not None:
            self.insert_gcode = gcode_macro.load_template(config, 'insert_gcode')
        self.run_always = config.getboolean('run_always', False)
        self.pause_delay = config.getfloat('pause_delay', .5, above=.0)  # Time to wait after pause
        self.event_delay = config.getfloat('event_delay', 3., above=0.)  # Time between generated events

        # Internal state
        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True

        # Register commands and event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

        # We are going to replace previous runout_helper mux commands with ours
        _replace_mux_command(self.gcode,
                             "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
                             self.cmd_QUERY_FILAMENT_SENSOR, desc=self.cmd_QUERY_FILAMENT_SENSOR_help)

        _replace_mux_command(self.gcode,
                             "SET_PAUSE_ON_RUNOUT", "SENSOR", self.name,
                             self.cmd_SET_PAUSE_ON_RUNOUT, desc=self.cmd_SET_PAUSE_ON_RUNOUT_help)

    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.  # Time to wait before first events are processed

    def _runout_event_handler(self, eventtime):
        # Pausing from inside an event requires that the pause portion
        # of pause_resume execute immediately.
        pause_prefix = ""
        if self.runout_pause:
            pause_resume = self.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            pause_prefix = "PAUSE\n"
            self.printer.get_reactor().pause(eventtime + self.pause_delay)
        self._exec_gcode(pause_prefix, self.runout_gcode)

    def _insert_event_handler(self, eventtime):
        self._exec_gcode("", self.insert_gcode)

    def _exec_gcode(self, prefix, template):
        try:
            gcode = prefix + (template.render() if template is not None else '')
            if gcode:
                self.gcode.run_script(gcode + "\nM400")
        except Exception:
            logging.exception("Error running filament_switch_sensor %s handler" % self.name)
        self.min_event_systime = self.reactor.monotonic() + self.event_delay

    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()

        if eventtime < self.min_event_systime or not self.sensor_enabled:
            # do not process during the initialization time, duplicates,
            # during the event delay time, while an event is running, or
            # when the sensor is disabled
            return

        # Determine "printing" status
        idle_timeout = self.printer.lookup_object("idle_timeout")
        is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"
        # Perform filament action associated with status change (if any)
        if is_filament_present:
            if self.run_always or not is_printing:
                # insert detected
                self.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: insert event detected, Time %.2f" %
                    (self.name, eventtime))
                self.reactor.register_callback(self._insert_event_handler)
        elif self.run_always or is_printing:
            # runout detected
            self.min_event_systime = self.reactor.NEVER
            logging.info(
                "Filament Sensor %s: runout event detected, Time %.2f" %
                (self.name, eventtime))
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled),
            "runout_pause": bool(self.runout_pause)}

    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % (self.name)
        else:
            msg = "Filament Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)

    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"

    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)

    cmd_SET_PAUSE_ON_RUNOUT_help = "Sets the pause on runout on/off"

    def cmd_SET_PAUSE_ON_RUNOUT(self, gcmd):
        self.runout_pause = gcmd.get_int("ENABLE", 1)


def _replace_mux_command(gcode, cmd, key, value, func, desc=None):
    # Remove existing, if it exists
    prev = gcode.mux_commands.get(cmd)
    if prev:
        prev_key, prev_values = prev
        if prev_key == key:
            del prev_values[value]

    # Register new
    gcode.register_mux_command(cmd, key, value, func, desc=desc)


def runout_helper_attach(obj, config):
    obj.runout_helper = KmmsRunoutHelper(config)
    obj.get_status = obj.runout_helper.get_status
    return obj


def load_config_prefix(config):
    config.get_printer().load_object(config, 'pause_resume')

    import extras.filament_switch_sensor
    obj = extras.filament_switch_sensor.SwitchSensor(config)

    return runout_helper_attach(obj, config)
