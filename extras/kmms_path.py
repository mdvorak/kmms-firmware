import logging
from typing import Optional

from configfile import ConfigWrapper
from klippy import Printer
from reactor import Reactor


class KmmsPathItem:
    DISTANCE_HISTORY = 5

    distances: list[float]

    def __init__(self, obj):
        self.obj = obj
        self.get_status = obj.get_status
        self.get_name = obj.get_name
        self.distances = []

        # Get fake status
        status = obj.get_status(Reactor.NEVER)

        # Build flags
        self.flags = KmmsPath.NONE
        if 'filament_detected' in status:
            self.flags |= KmmsPath.SENSOR
        if 'pressure' in status:
            self.flags |= KmmsPath.BACKPRESSURE
        if hasattr(obj, 'move'):
            self.flags |= KmmsPath.EXTRUDER
        if hasattr(obj, 'sync_to_extruder'):
            self.flags |= KmmsPath.SYNCING_EXTRUDER
        if hasattr(obj, 'get_path_items'):
            self.flags |= KmmsPath.PATH

    def has_flag(self, flag: int) -> bool:
        return (flag & self.flags) == flag

    def filament_detected(self, eventtime) -> Optional[bool]:
        status = self.get_status(eventtime)
        return status['filament_detected'] if ('enabled' not in status or status['enabled']) else None

    def note_distance(self, distance: float):
        self.distances.append(distance)
        if len(self.distances) > self.DISTANCE_HISTORY:
            self.distances.pop(0)

    def get_distance(self):
        count = len(self.distances)
        return sum(self.distances) / count if count else 0.

    def get_object(self):
        return self.obj

    def __str__(self):
        return self.get_name()


class KmmsPath:
    KmmsPathItem = KmmsPathItem

    NONE = 0
    SENSOR = 1
    EXTRUDER = 1 << 1
    SYNCING_EXTRUDER = 1 << 2
    BACKPRESSURE = (1 << 3) | SENSOR
    PATH = 1 << 4

    printer: Printer
    _items: list[KmmsPathItem]

    def __init__(self, config: ConfigWrapper):
        self.logger = logging.getLogger(config.get_name().replace(' ', '.'))
        self.printer = config.get_printer()

        self.full_name = config.get_name()
        self.name = config.get_name().split()[-1]

        self.path = list(filter(None, (p.strip() for p in config.getlist('path', sep='\n'))))
        self._names = {self.full_name}
        self._items = []

        self.printer.register_event_handler("kmms:init", self._handle_init)

    def _handle_init(self):
        # Load objects
        for obj_name in self.path:
            self.lookup_object(obj_name)

    def _append(self, item: KmmsPathItem):
        name = item.get_name()
        if name in self._names:
            raise self.printer.config_error("'%s' already contains '%s'" % (self.full_name, name))

        self.logger.info('Adding object %s', name)
        self._names.add(name)
        self._items.append(item)

    def get_name(self):
        return self.full_name

    def add_object(self, obj):
        item = KmmsPathItem(obj)
        self._append(item)

        # If obj is another path, explode it
        if item.has_flag(self.PATH):
            for nested in obj.get_path_items():
                self._append(nested)

    def lookup_object(self, name):
        self.add_object(self.printer.lookup_object(name.strip()))

    def get_path_items(self) -> list[KmmsPathItem]:
        return self._items

    def get_objects(self, flag=NONE, start=0, stop=None) -> list[object]:
        return [i.get_object() for i in self._items[start:stop] if i.has_flag(flag)]

    def find_object(self, obj, start=0, stop=None) -> (int, Optional[KmmsPathItem]):
        for i, item in enumerate(self._items[start:stop]):
            if item.get_object() is obj:
                return start + i, item
        return -1, None

    def find_path_position(self, eventtime) -> (int, Optional[KmmsPathItem]):
        result = (-1, None)
        for i, item in enumerate(self._items):
            filament_detected = item.filament_detected(eventtime) if item.has_flag(self.SENSOR) else None
            if filament_detected:
                result = (i, item)
            elif filament_detected is not None:
                # Stop on first empty sensor - this skips components that does not track filament
                break
        self.logger.debug('Found position at %d', result[0])
        return result

    def find_path_items(self, flag=NONE, start=0, stop=None) -> list[(int, KmmsPathItem)]:
        self.logger.debug('Finding all %d from %d to %s', flag, start, stop)
        return [(start + i, obj) for i, obj in enumerate(self._items[start:stop]) if obj.has_flag(flag)]

    def find_path_next(self, flag: int, start=0, stop=None) -> (int, Optional[KmmsPathItem]):
        self.logger.debug('Finding next %d from %d to %s', flag, start, stop)
        for i, item in enumerate(self._items[start:stop]):
            if item.has_flag(flag):
                return start + i, item
        return -1, None

    def find_path_last(self, flag: int, start: int, stop=0) -> (int, Optional[KmmsPathItem]):
        if start < stop:
            raise ValueError('start must be >= stop')

        self.logger.debug('Finding last %d from %d to %s', flag, start, stop)
        for i in reversed(range(stop, start)):
            item = self._items[i]
            if item.has_flag(flag):
                return i, item
        return -1, None

    def __getitem__(self, pos: int) -> Optional[KmmsPathItem]:
        return self._items[pos]

    def __len__(self):
        return len(self._items)


def load_config_prefix(config):
    return KmmsPath(config)


def load_config(config):
    return KmmsPath
