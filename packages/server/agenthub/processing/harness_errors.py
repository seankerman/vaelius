class HarnessError(RuntimeError):
    """Sanitized harness error code, never a provider payload."""
    def __init__(self, code, *, outcome='uncertain', usage=None):
        super().__init__(code)
        self.outcome=outcome
        self.usage={k:v for k,v in (usage or {}).items() if k in {
            'input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens',
            'reasoning_output_tokens'} and type(v) is int and v>=0}
