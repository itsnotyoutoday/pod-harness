class LipsyncPipeline:
    def __init__(self, proc): self.image_processor = proc
    def __call__(self, *a, **k): return "ran"
