class JobError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
