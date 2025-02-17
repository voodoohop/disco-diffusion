import math
import sys

import midas_utils
import numpy as np
import py3d_tools as p3d
import scipy
import torch
import torchvision
from PIL import Image

try:
    from infer import InferenceHelper
except:
    print("disco_xform_utils.py failed to import InferenceHelper. Please ensure that AdaBins directory is in the path (i.e. via sys.path.append('./AdaBins') or other means).")
    sys.exit()

MAX_ADABINS_AREA = 500000

infer_helper = None

@torch.no_grad()
def transform_image_3d(img_filepath, midas_model, midas_transform, device, rot_mat=torch.eye(3).unsqueeze(0), translate=(0.,0.,-0.04), near=2000, far=20000, fov_deg=60, padding_mode='border', sampling_mode='bicubic', midas_weight = 0.3, return_depth_map=False):
    global infer_helper
    
    img_pil = Image.open(open(img_filepath, 'rb')).convert('RGB')
    w, h = img_pil.size
    image_tensor = torchvision.transforms.functional.to_tensor(img_pil).to(device)

    use_adabins = midas_weight < 1.0

    if use_adabins:
        # AdaBins
        """
        predictions using nyu dataset
        """
        adabins_depth_np = None
        print("Running AdaBins depth estimation implementation...")
        if infer_helper is None:
            infer_helper = InferenceHelper(dataset='nyu')

        image_pil_area = w*h
        if image_pil_area > MAX_ADABINS_AREA:
            scale = math.sqrt(MAX_ADABINS_AREA) / math.sqrt(image_pil_area)
            depth_input = img_pil.resize((int(w*scale), int(h*scale)), Image.LANCZOS) # LANCZOS is supposed to be good for downsampling.
        else:
            depth_input = img_pil
        try:
            _, adabins_depth = infer_helper.predict_pil(depth_input)
            adabins_depth = torchvision.transforms.functional.resize(torch.from_numpy(adabins_depth), image_tensor.shape[-2:], interpolation=torchvision.transforms.functional.InterpolationMode.BICUBIC).squeeze().to(device)
            adabins_depth_np = adabins_depth.cpu().numpy()
        except Exception as e:
            print("Failed to run AdaBins depth estimation. Falling back to default depth estimation.", e)
            pass    
        print("Done AdaBins")

    torch.cuda.empty_cache()

    # MiDaS
    img_midas = midas_utils.read_image(img_filepath)
    img_midas_input = midas_transform({"image": img_midas})["image"]
    midas_optimize = True

    # MiDaS depth estimation implementation
    print("Running MiDaS depth estimation implementation...")
    sample = torch.from_numpy(img_midas_input).float().to(device).unsqueeze(0)
    if midas_optimize==True and device == torch.device("cuda"):
        sample = sample.to(memory_format=torch.channels_last)  
        sample = sample.half()
    prediction_torch = midas_model.forward(sample)
    prediction_torch = torch.nn.functional.interpolate(
            prediction_torch.unsqueeze(1),
            size=img_midas.shape[:2],
            mode="bicubic",
            align_corners=False,
        ).squeeze()
    prediction_np = prediction_torch.clone().cpu().numpy()

    print("Finished depth estimation.")
    torch.cuda.empty_cache()

    # MiDaS makes the near values greater, and the far values lesser. Let's reverse that and try to align with AdaBins a bit better.
    prediction_np = np.subtract(50.0, prediction_np)
    prediction_np = prediction_np / 19.0

    if use_adabins and adabins_depth_np is not None:
        adabins_weight = 1.0 - midas_weight
        depth_map = prediction_np*midas_weight + adabins_depth_np*adabins_weight
    else:
        depth_map = prediction_np

    depth_map = np.expand_dims(depth_map, axis=0)
    depth_tensor = torch.from_numpy(depth_map).squeeze().to(device)

    #print("depth_map", depth_map.shape, "min", np.min(depth_map), "max", np.max(depth_map))
    
    #print("rot_mat", rot_mat, "translate", translate)
    
    # calculate min and max of the depth map
    depth_min = np.min(depth_map)
    depth_max = np.max(depth_map)

    # create a normalized copy of the depth map
    depth_map_normalized = (depth_map[0] - depth_min) / (depth_max - depth_min)

    # depth map normalized is of shape (H, W)

    pixel_aspect = 1.0 # really.. the aspect of an individual pixel! (so usually 1.0)
    persp_cam_old = p3d.FoVPerspectiveCameras(near, far, pixel_aspect, fov=fov_deg, degrees=True, device=device)
    persp_cam_new = p3d.FoVPerspectiveCameras(near, far, pixel_aspect, fov=fov_deg, degrees=True, R=rot_mat, T=torch.tensor([translate]), device=device)

    # range of [-1,1] is important to torch grid_sample's padding handling
    y,x = torch.meshgrid(torch.linspace(-1.,1.,h,dtype=torch.float32,device=device),torch.linspace(-1.,1.,w,dtype=torch.float32,device=device))
    z = torch.as_tensor(depth_tensor, dtype=torch.float32, device=device)
    xyz_old_world = torch.stack((x.flatten(), y.flatten(), z.flatten()), dim=1)

    # Transform the points using pytorch3d. With current functionality, this is overkill and prevents it from working on Windows.
    # If you want it to run on Windows (without pytorch3d), then the transforms (and/or perspective if that's separate) can be done pretty easily without it.
    xyz_old_cam_xy = persp_cam_old.get_full_projection_transform().transform_points(xyz_old_world)[:,0:2]
    xyz_new_cam_xy = persp_cam_new.get_full_projection_transform().transform_points(xyz_old_world)[:,0:2]

    offset_xy = xyz_new_cam_xy - xyz_old_cam_xy
    # affine_grid theta param expects a batch of 2D mats. Each is 2x3 to do rotation+translation.
    identity_2d_batch = torch.tensor([[1.,0.,0.],[0.,1.,0.]], device=device).unsqueeze(0)
    # coords_2d will have shape (N,H,W,2).. which is also what grid_sample needs.
    coords_2d = torch.nn.functional.affine_grid(identity_2d_batch, [1,1,h,w], align_corners=False)
    offset_coords_2d = coords_2d - torch.reshape(offset_xy, (h,w,2)).unsqueeze(0)
    new_image = torch.nn.functional.grid_sample(image_tensor.add(1/512 - 0.0001).unsqueeze(0), offset_coords_2d, mode=sampling_mode, padding_mode=padding_mode, align_corners=False)
    img_pil = torchvision.transforms.ToPILImage()(new_image.squeeze().clamp(0,1.))

    torch.cuda.empty_cache()

    if return_depth_map:
        return img_pil, depth_map_normalized
    else:
        return img_pil
