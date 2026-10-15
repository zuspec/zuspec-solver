"""Translation error type."""


class TranslateError(Exception):
    """A constraint the solver cannot take.

    Carries an optional ``loc`` (source location). A backend re-raises it as
    its own error type: be-bc as a ``LoweringError``, so a refusal reads the
    same whichever package found it.
    """

    def __init__(self, message, loc=None):
        super().__init__(message)
        self.loc = loc
