# -*- coding: UTF-8 -*-
import argparse
import time
from pathlib import Path

import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random
import copy
import numpy as np
import math

from utils.keypoint_scores import PoseRunningScore
from utils.datasets import img2label_paths
from models.experimental import attempt_load
from utils.datasets import LoadStreams, LoadImages, letterbox
from utils.general import check_img_size, non_max_suppression_landmark, apply_classifier, scale_coords, xyxy2xywh, \
    strip_optimizer, set_logging, increment_path
from utils.plots import plot_one_box
from utils.torch_utils import select_device, load_classifier, time_synchronized

from models.yolo import Model
import yaml

import warnings

warnings.filterwarnings("ignore")


def load_model(weights, cfg_path, device):
    model = attempt_load(weights, map_location=device)  # load FP32 model
    # model = Model(cfg_path, ch=3, nc=1 ).to(device)  # create
    # ckpt = torch.load(weights, map_location='cpu')  # load
    # state_dict = ckpt['model'].float().state_dict()
    # model.load_state_dict(state_dict)
    # model.eval()
    return model


def scale_coords_landmarks(img1_shape, coords, img0_shape, ratio_pad=None):
    # Rescale coords (xyxy) from img1_shape to img0_shape
    if ratio_pad is None:  # calculate from img0_shape
        gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])  # gain  = old / new
        pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2  # wh padding
    else:
        gain = ratio_pad[0][0]
        pad = ratio_pad[1]

    coords[:, [0, 2, 4, 6]] -= pad[0]  # x padding
    coords[:, [1, 3, 5, 7]] -= pad[1]  # y padding
    coords[:, :8] /= gain
    # clip_coords(coords, img0_shape)
    coords[:, 0].clamp_(0, img0_shape[1])  # x1
    coords[:, 1].clamp_(0, img0_shape[0])  # y1
    coords[:, 2].clamp_(0, img0_shape[1])  # x2
    coords[:, 3].clamp_(0, img0_shape[0])  # y2
    coords[:, 4].clamp_(0, img0_shape[1])  # x3
    coords[:, 5].clamp_(0, img0_shape[0])  # y3
    coords[:, 6].clamp_(0, img0_shape[1])  # x4
    coords[:, 7].clamp_(0, img0_shape[0])  # y4
    return coords


def get_ther(landmarkspoints, class_num):
    class_num = int(class_num)
    if class_num > 5:
        pMax = (
        (landmarkspoints[0][0] + landmarkspoints[1][0]) / 2, (landmarkspoints[0][1] + landmarkspoints[1][1]) / 2)
        pMin = (
        (landmarkspoints[2][0] + landmarkspoints[3][0]) / 2, (landmarkspoints[2][1] + landmarkspoints[3][1]) / 2)
    elif class_num > 1:
        pMin = (
        (landmarkspoints[0][0] + landmarkspoints[1][0]) / 2, (landmarkspoints[0][1] + landmarkspoints[1][1]) / 2)
        pMax = (
        (landmarkspoints[2][0] + landmarkspoints[3][0]) / 2, (landmarkspoints[2][1] + landmarkspoints[3][1]) / 2)

    # x, y = pMax[0] - pMin[0], pMin[1] - pMax[1]
    x, y = pMin[0] - pMax[0], pMax[1] - pMin[1]
    ther = math.atan2(x, y)

    return int(ther / math.pi * 180)


def show_results(img, xywh, conf, landmarks, class_num):
    h, w, c = img.shape
    tl = 2 or round(0.002 * (h + w) / 2) + 1  # line/font thickness
    x1 = int(xywh[0] * w - 0.5 * xywh[2] * w)
    y1 = int(xywh[1] * h - 0.5 * xywh[3] * h)
    x2 = int(xywh[0] * w + 0.5 * xywh[2] * w)
    y2 = int(xywh[1] * h + 0.5 * xywh[3] * h)
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), thickness=tl, lineType=cv2.LINE_AA)

    clors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]
    landmarkspoints = []
    for i in range(4):
        point_x = int(landmarks[2 * i] * w)
        point_y = int(landmarks[2 * i + 1] * h)
        cv2.circle(img, (point_x, point_y), tl + 1, clors[i], -1)
        landmarkspoints.append([point_x, point_y])

    mask = np.zeros((h, w, c), dtype=np.uint8)

    cv2.fillConvexPoly(mask, np.array(landmarkspoints), (0, 100, 255))  # 绘制 地面投影
    img = cv2.addWeighted(img, 1, mask, 0.5, 0)

    ther = get_ther(landmarkspoints, class_num)

    tf = max(tl - 1, 1)  # font thickness
    # label = str(int(class_num)) + ': ' + str(conf)[:5] + '&:' +str(ther)
    label = str(int(class_num)) + ': ' + str(conf)[:5]
    print('label', str(int(class_num)) + ': ' + str(conf)[:5] + ' 航向角:' + str(ther))
    cv2.putText(img, label, (x1, y1 - 2), 0, tl / 3, [225, 255, 255], thickness=tf, lineType=cv2.LINE_AA)
    return img

def get_results(img, xywh, conf, landmarks, class_num):
    h, w, c = img.shape
    tl = 2 or round(0.002 * (h + w) / 2) + 1  # line/font thickness
    x1 = int(xywh[0] * w - 0.5 * xywh[2] * w)
    y1 = int(xywh[1] * h - 0.5 * xywh[3] * h)
    x2 = int(xywh[0] * w + 0.5 * xywh[2] * w)
    y2 = int(xywh[1] * h + 0.5 * xywh[3] * h)
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), thickness=tl, lineType=cv2.LINE_AA)

    clors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]
    landmarkspoints = []
    for i in range(4):
        point_x = int(landmarks[2 * i] * w)
        point_y = int(landmarks[2 * i + 1] * h)
        cv2.circle(img, (point_x, point_y), tl + 1, clors[i], -1)
        landmarkspoints.append([point_x, point_y])

    return landmarkspoints

def warpimage(img, pts1):
    # right_bottom, left_bottom, left up , left_up
    # pts1 = np.float32([[56,65],[368,52],[28,387],[389,390]])
    pts2 = np.float32([[400, 200], [0, 200], [0, 0], [400, 0]])
    h, w, c = img.shape
    M = cv2.getPerspectiveTransform(pts1, pts2)
    dst = cv2.warpPerspective(img, M, (400, 200))
    return dst

def read_xml_list(file):
    file_list = []
    with open(file, 'r') as file_r:
        lines = file_r.readlines()
        for line in lines:
            value = line.strip()
            file_list.append(value)
    return file_list

def detect_one(model, image_path, device):
    # Load model
    img_size = 320
    # img_size = 416
    conf_thres = 0.5
    iou_thres = 0.5
    image_list=read_xml_list(image_path)
    gt_batch=[]
    pred_batch=[]
    oks=[]
    for img_pth in image_list:
        img_label_path=img_pth.replace('JPEGImages','labels1').replace('jpg','txt').replace('png','txt')
        with open(img_label_path, 'r') as f:
            l = [x.split() for x in f.read().strip().splitlines()]

        if any([len(x) > 5 for x in l]):  # is landmarks
            for x1 in l:
                if len(x1) == 5:
                    x1.extend(np.array(np.zeros((1, 8), dtype=np.float32) - 1, dtype=np.float32).tolist())
            classes = np.array([x[0] for x in l], dtype=np.float32)
            rects = [np.array(x[1:5], dtype=np.float32) for x in l]
            landmarks_t = [np.array(x[5:13], dtype=np.float32).reshape(-1, 2) for x in l]  # (cls, xy1...)
        else:
            classes = np.array([x[0] for x in l], dtype=np.float32)
            rects = [np.array(x[1:5], dtype=np.float32) for x in l]
            landmarks_t = [np.array([-1, -1, -1, -1, -1, -1, -1, -1], dtype=np.float32).reshape(-1, 2) for x in
                         l]  # (cls, xy1...)


        orgimg = cv2.imread(img_pth)  # BGR
        img0 = copy.deepcopy(orgimg)
        assert orgimg is not None, 'Image Not Found ' + image_path
        h0, w0 = orgimg.shape[:2]  # orig hw
        r = img_size / max(h0, w0)  # resize image to img_size
        if r != 1:  # always resize down, only resize up if training with augmentation
            interp = cv2.INTER_AREA if r < 1 else cv2.INTER_LINEAR
            img0 = cv2.resize(img0, (int(w0 * r), int(h0 * r)), interpolation=interp)

        imgsz = check_img_size(img_size, s=model.stride.max())  # check img_size

        img = letterbox(img0, new_shape=imgsz)[0]
        # Convert
        img = img[:, :, ::-1].transpose(2, 0, 1).copy()  # BGR to RGB, to 3x416x416

        # Run inference
        t0 = time.time()

        img = torch.from_numpy(img).to(device)
        img = img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)

        # Inference
        t1 = time_synchronized()
        pred = model(img)[0]
        # print('pred: ', pred.shape)
        # Apply NMS
        # pred = non_max_suppression_landmark(pred, conf_thres, iou_thres)
        pred = non_max_suppression_landmark(pred, conf_thres, iou_thres, multi_label=True)
        # print('nms: ', pred)
        t2 = time_synchronized()

        # print('img.shape: ', img.shape)
        # print('orgimg.shape: ', orgimg.shape)

        results_pre_all = []
        # Process detections
        for i, det in enumerate(pred):  # detections per image
            gn = torch.tensor(orgimg.shape)[[1, 0, 1, 0]].to(device)  # normalization gain whwh
            gn_lks = torch.tensor(orgimg.shape)[[1, 0, 1, 0, 1, 0, 1, 0]].to(device)  # normalization gain landmarks
            if len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], orgimg.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class

                det[:, 15:23] = scale_coords_landmarks(img.shape[2:], det[:, 15:23], orgimg.shape).round()


                for j in range(det.size()[0]):
                    xywh = (xyxy2xywh(torch.tensor(det[j, :4]).view(1, 4)) / gn).view(-1).tolist()
                    conf = det[j, 4].cpu().numpy()
                    landmarks = (det[j, 15:23].view(1, 8) / gn_lks).view(-1).tolist()
                    # class_num = det[j, 23].cpu().numpy()

                    results_pre = get_results(orgimg, xywh, conf, landmarks, 10)
                    results_pre_all.append(results_pre)

        landmarks_t=np.squeeze(np.array(landmarks_t))
        landmarks_t[:, 0] = landmarks_t[:, 0] * 640
        landmarks_t[:, 1] = landmarks_t[:, 1] * 400

        results_pre_all=np.squeeze(np.array(results_pre_all))

        if len(landmarks_t.shape)==2:
            landmarks_t=np.expand_dims(landmarks_t, 0)
        if len(results_pre_all.shape)==2:
            results_pre_all=np.expand_dims(results_pre_all,0)
        oks.append(PoseRunningScore.compute_oks(None,landmarks_t,results_pre_all))
        # print(PoseRunningScore.compute_oks(None,landmarks_t,results_pre_all))
        gt_batch.append(landmarks_t)
        pred_batch.append(results_pre_all)
    score=PoseRunningScore()
    score.update(gt_batch,pred_batch,oks)
    print(score.get_mAP())

                # h,w,c = orgimg.shape
                # points = np.array(landmarks, dtype=np.float32).reshape(-1, 2)

                # print('points: ', points)
                # dst = warpimage(orgimg, points)
                # cv2.imwrite('./result_warp.jpg', dst)

    # Stream results
    print(f'Done. ({time.time() - t0:.3f}s)')

    # cv2.imshow('orgimg', orgimg)
    # cv2.imwrite('./result.jpg', orgimg)
    # if cv2.waitKey(0) == ord('q'):  # q to quit
    #    raise StopIteration


if __name__ == '__main__':
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    weights = './runs/train/exp125/weights/best.pt'
    cfg_path = './models/yolov5s.yaml'
    model = load_model(weights, cfg_path, device)
    # root = '/home/xialuxi/work/dukto/data/CCPD2020/CCPD2020/images/test/'
    image_path = '/mnt/sdb2/dataset/keypoint2/train.txt'
    # image_path = '/home/wqg/data/maxvision_data/ADAS/1964/train/images/0016.jpg'
    detect_one(model, image_path, device)
    print('over')


