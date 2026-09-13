"""Immutable validated JSON trees that may be shared by snapshot revisions.

Construction references change by replacement, unlike a chunk's mutable voxel
buffer. Freezing each reference once prevents accidental mutation while letting
the snapshot model reuse its validated representation across later storeys.
"""


def _immutable(*_args, **_kwargs):
    raise TypeError('Frozen JSON values must be replaced, not mutated.')


class FrozenJsonDict(dict):
    def __init__(self, value):
        if getattr(self, '_frozen_initialized', False):
            _immutable()
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise TypeError('Only normalized JSON objects can be frozen.')
        dict.__init__(self, {key: freeze_json(item) for key, item in value.items()})
        self._frozen_initialized = True

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class FrozenJsonList(list):
    def __init__(self, value):
        if getattr(self, '_frozen_initialized', False):
            _immutable()
        list.__init__(self, (freeze_json(item) for item in value))
        self._frozen_initialized = True

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def freeze_json(value):
    if isinstance(value, (FrozenJsonDict, FrozenJsonList)):
        return value
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return FrozenJsonDict(value)
    if isinstance(value, list):
        return FrozenJsonList(value)
    raise TypeError('Only normalized JSON can become a frozen snapshot reference.')
