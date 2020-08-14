import torch
import cv2
import numpy as np
from skimage import transform

from .utils.alignment import get_reference_facial_points, FaceWarpException
from .utils.box_utils import decode, decode_landmark, prior_box, nms
from .models.retinaface import RetinaFace

cfg = {
    'name': 'Resnet50',
    'min_sizes': [[16, 32], [64, 128], [256, 512]],
    'steps': [8, 16, 32],
    'variance': [0.1, 0.2],
    'clip': False,
    'loc_weight': 2.0,
    'gpu_train': True,
    'batch_size': 24,
    'ngpu': 4,
    'epoch': 100,
    'decay1': 70,
    'decay2': 90,
    'image_size': 840,
    'pretrain': False,
    'return_layers': {'layer2': 1, 'layer3': 2, 'layer4': 3},
    'in_channel': 256,
    'out_channel': 256
}


class FaceDetector:

    def __init__(self, weight_path, device='cpu', confidence_threshold=0.99,
                 top_k=5000, nms_threshold=0.4, keep_top_k=750, face_size=(112, 112)):
        """
        RetinaFace Detector with 5points landmarks
        Args:
            weight_path: path of network weight
            device: running device (cuda, cpu)
            face_size: final face size
            face_padding: padding for bounding boxes
        """
        # setting for model
        model = RetinaFace(cfg, phase='test')
        state_dict = torch.load(weight_path)
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in state_dict.items():
            name = k[7:] # remove `module.`
            new_state_dict[name] = v
        model.load_state_dict(new_state_dict)
        model.to(device).eval()
#        model.half()
        self.model = model
        self.device = device
        self.cfg = cfg
        # setting for face detection
        self.thresh = confidence_threshold
        self.top_k = top_k
        self.nms_thresh = nms_threshold
        self.keep_top_k = keep_top_k
        # setting for face align
        self.trans = transform.SimilarityTransform()
        self.out_size = face_size
        self.ref_pts = get_reference_facial_points(output_size=face_size)
        print('from FaceDetector: weights loaded')
        return

    def preprocessor(self, img_raw):
        img = torch.tensor(img_raw, dtype=torch.float32).to(self.device)
        #img = torch.tensor(img_raw, dtype=torch.float16).to(self.device)
        scale = torch.Tensor([img.shape[1], img.shape[0], img.shape[1], img.shape[0]]).to(self.device)
        img -= torch.tensor([104, 117, 123]).to(self.device)
        img = img.permute(2, 0, 1).unsqueeze(0)
        return img, scale

    def detect_faces(self, img_raw):
        """
        get a image from ndarray, detect faces in image
        Args:
            img_raw: original image from cv2(BGR) or PIL(RGB)
        Notes:
            coordinate is corresponding to original image
            and type of return image is corresponding to input(cv2, PIL)
        Returns:
            boxes:
                faces bounding box for each face
            scores:
                percentage of each face
            landmarks:
                faces landmarks for each face
        """

        img, scale = self.preprocessor(img_raw)
        # tic = time.time()
        with torch.no_grad():
            loc, conf, landmarks = self.model(img)  # forward pass
            # print('net forward time: {:.4f}'.format(time.time() - tic))

        priors = prior_box(self.cfg, image_size=img.shape[2:]).to(self.device)
        boxes = decode(loc.data.squeeze(0), priors, self.cfg['variance'])
        boxes = boxes * scale
        scores = conf.squeeze(0)[:, 1]
        landmarks = decode_landmark(landmarks.squeeze(0), priors, self.cfg['variance'])
        scale1 = torch.Tensor([img.shape[3], img.shape[2], img.shape[3], img.shape[2],
                               img.shape[3], img.shape[2], img.shape[3], img.shape[2],
                               img.shape[3], img.shape[2]]).to(self.device)
        landmarks = landmarks * scale1

        # ignore low scores
        index = torch.where(scores > self.thresh)[0]
        boxes = boxes[index]
        landmarks = landmarks[index]
        scores = scores[index]

        # keep top-K before NMS
        order = scores.argsort(dim=0, descending=True)[:self.top_k]
        boxes = boxes[order]
        landmarks = landmarks[order]
        scores = scores[order]

        # Do NMS
        keep = nms(boxes, scores, self.nms_thresh)
        boxes = torch.abs(boxes[keep, :])
        scores = scores[:, None][keep, :]
        landmarks = landmarks[keep, :].reshape(-1, 5, 2)

        # # keep top-K faster NMS
        landmarks = landmarks[:self.keep_top_k, :]
        scores = scores[:self.keep_top_k, :]
        boxes = boxes[:self.keep_top_k, :]

        return boxes, scores, landmarks

    def detect_align(self, img):
        """
        get a image from ndarray, detect faces in image,
        cropped face and align face
        Args:
            img: original image from cv2(BGR) or PIL(RGB)
        Notes:
            coordinate is corresponding to original image
            and type of return image is corresponding to input(cv2, PIL)

        Returns:
            faces:
                a tensor(n, 112, 112, 3) of faces that aligned
            boxes:
                face bounding box for each face
            landmarks:
                face landmarks for each face
        """
        boxes, scores, landmarks = self.detect_faces(img)

        warped = []
        for src_pts in landmarks:
            if max(src_pts.shape) < 3 or min(src_pts.shape) != 2:
                raise FaceWarpException('facial_pts.shape must be (K,2) or (2,K) and K>2')

            if src_pts.shape[0] == 2:
                src_pts = src_pts.T

            if src_pts.shape != self.ref_pts.shape:
                raise FaceWarpException('facial_pts and reference_pts must have the same shape')

            self.trans.estimate(src_pts.cpu().numpy(), self.ref_pts)
            face_img = cv2.warpAffine(img, self.trans.params[0:2, :], self.out_size)
            warped.append(face_img)

        faces = torch.tensor(warped).to(self.device)
        return faces, boxes, scores, landmarks
