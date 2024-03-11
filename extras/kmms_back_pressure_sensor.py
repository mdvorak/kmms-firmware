import logging


class BackPressureSensor:
    def __init__(self, config):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]


def load_config_prefix(config):
    return BackPressureSensor(config)
