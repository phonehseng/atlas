"""CLIP ViT-B/32 image and text encoders on ONNX Runtime (DirectML on any DX12 GPU, CPU otherwise)."""
import threading

import numpy as np

from . import deps

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32).reshape(1, 3, 1, 1)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32).reshape(1, 3, 1, 1)
CONTEXT = 77
BATCH = 64


class Clip:
    def __init__(self, gpu=True):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.set_default_logger_severity(3)
        avail = ort.get_available_providers()
        want = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider") if gpu and p in avail]
        self.provider = want[0] if want else "CPUExecutionProvider"

        def session(name):
            so = ort.SessionOptions()
            so.enable_mem_pattern = False  # required by DirectML
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.intra_op_num_threads = 2
            so.log_severity_level = 3
            # The fp16 export trips a CPU layer-norm fusion at the highest level; basic is fine (and still fast).
            so.graph_optimization_level = (ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                                           if self.provider != "CPUExecutionProvider"
                                           else ort.GraphOptimizationLevel.ORT_ENABLE_BASIC)
            return ort.InferenceSession(str(deps.MODEL_DIR / name), so, providers=[self.provider])

        self.vision = session("vision_model_fp16.onnx")
        self.textm = session("text_model_fp16.onnx")
        self.tok = Tokenizer.from_file(str(deps.MODEL_DIR / "tokenizer.json"))
        self.tok.enable_truncation(CONTEXT)
        self._lock = threading.Lock()
        self._text_cache = {}

    @staticmethod
    def _norm(x):
        return (x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-6)).astype(np.float32)

    def images(self, frames):
        """frames: uint8 [N, 224, 224, 3] RGB -> L2-normalised float32 [N, 512]."""
        out = []
        for i in range(0, len(frames), BATCH):
            x = frames[i:i + BATCH].astype(np.float32).transpose(0, 3, 1, 2) / 255.0
            x = (x - MEAN) / STD
            with self._lock:
                out.append(self.vision.run(None, {"pixel_values": x})[0])
        return self._norm(np.concatenate(out)) if out else np.zeros((0, 512), np.float32)

    def texts(self, texts):
        """list[str] -> L2-normalised float32 [N, 512] (cached)."""
        need = [t for t in dict.fromkeys(texts) if t not in self._text_cache]
        if need:
            enc = self.tok.encode_batch([t.lower() for t in need])
            n = max(len(e.ids) for e in enc)
            # CLIP pads with the end-of-text token; the model pools at the first (highest-id) one.
            ids = np.full((len(enc), n), 49407, np.int64)
            for i, e in enumerate(enc):
                ids[i, :len(e.ids)] = e.ids
            with self._lock:
                emb = self._norm(self.textm.run(None, {"input_ids": ids})[0])
            self._text_cache.update(zip(need, emb))
        return np.stack([self._text_cache[t] for t in texts])

    def query(self, text):
        """A search phrase, averaged over a few phrasings (prompt ensemble)."""
        text = text.strip()
        e = self.texts([text, f"a photo of {text}", f"a film still of {text}", f"a scene showing {text}"]).mean(0)
        return self._norm(e)
