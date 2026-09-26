class StudioError(ValueError):
    status_code = 409


class StudioNotFound(StudioError):
    status_code = 404
