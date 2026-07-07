class CounterError(Exception):
    pass

class InvalidCounterKeyError(CounterError, ValueError):
    pass

class NegativeCountError(CounterError, ValueError):
    pass
