class FaceDetector:
    def __init__(self, app): self.app = app
    def __call__(self, frame, threshold=0.5):
        faces = self.app.get(frame)
        if not faces: return None, None
        f = faces[0]; return tuple(f.bbox), [[f.bbox[0], f.bbox[1]]]
