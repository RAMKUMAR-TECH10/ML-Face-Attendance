import torch
import numpy as np
from facenet_pytorch import MTCNN, InceptionResnetV1

class RecognitionEngine:
    def __init__(self, threshold=0.8):
        self.threshold = threshold
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

        self.mtcnn = MTCNN(
            image_size=160,
            margin=0,
            min_face_size=20,
            thresholds=[0.6, 0.7, 0.7],
            factor=0.709,
            post_process=True,
            device=self.device
        )

        self.resnet = InceptionResnetV1(
            pretrained='vggface2',
            classify=False,
            num_classes=None
        ).eval().to(self.device)

    def get_locations(self, frame):
        rgb_frame = frame[:, :, ::-1].copy()
        boxes, probs, landmarks = self.mtcnn.detect(rgb_frame, landmarks=True)

        if boxes is None or len(boxes) == 0:
            return []

        locations = []
        for box in boxes:
            x1, y1, x2, y2 = box[:4]
            locations.append((int(y1), int(x2), int(y2), int(x1)))

        return locations

    def get_encodings(self, frame, face_locations=None):
        rgb_frame = frame[:, :, ::-1].copy()

        try:
            aligned = self.mtcnn(rgb_frame)

            if aligned is None:
                return []

            if aligned.ndim == 3:
                aligned = aligned.unsqueeze(0)

            aligned = aligned.to(self.device)

            with torch.no_grad():
                embeddings = self.resnet(aligned)

            embeddings = embeddings.cpu().numpy()
            embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)

            return [emb for emb in embeddings]
        except Exception as e:
            print(f"[ENGINE ERROR] FaceNet encoding failed: {e}")
            return []

    def compare_faces(self, known_encodings, face_encoding, threshold=None):
        if not known_encodings or len(known_encodings) == 0:
            return None, 2.0

        if threshold is None:
            threshold = self.threshold

        valid_encodings = []
        valid_indices = []
        for i, enc in enumerate(known_encodings):
            arr = np.asarray(enc, dtype=np.float64)
            if arr.ndim == 1 and arr.shape[0] == 512:
                valid_encodings.append(arr)
                valid_indices.append(i)

        if not valid_encodings:
            return None, 2.0

        face_encoding = np.asarray(face_encoding, dtype=np.float64)
        face_encoding = face_encoding / np.linalg.norm(face_encoding)

        distances = []
        for enc in valid_encodings:
            dist = np.linalg.norm(face_encoding - enc)
            distances.append(dist)

        distances = np.array(distances)
        min_distance = np.min(distances)

        if min_distance < threshold:
            local_index = np.argmin(distances)
            original_index = valid_indices[local_index]
            return original_index, min_distance
        return None, min_distance
