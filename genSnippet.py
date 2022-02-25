import argparse
import logging
import random
import time
import threading
import weakref
from queue import Queue, Empty

import numpy as np
import h5py

import fixpath
import carla

class Vehicle:
    '''Creates and spawn a vehicle with a lidar sensor'''

    #Class variable that stores references to all instances
    instances = []

    def __init__(self, transform, world, args):
        '''Try to spawn vehicle at given transform, may fail due to collision. If it doesnt, spawns lidar sensor and add object to instances'''

        #try to spawn vehicle
        self.world = world
        self.vehicle = world.try_spawn_actor(self.get_random_blueprint(), transform)
        if self.vehicle is None:
            return
        Vehicle.instances.append(self)
        self.vehicle.set_autopilot(args.no_autopilot)
        self.id = self.vehicle.id
        self.sensorQueue = Queue()

        #create lidar sensor and registers callback
        #lidar height is the height of the vehicle plus the necessary to avoid any points on the roof of the vehicle given the lower fov angle
        hp = max(self.vehicle.bounding_box.extent.x,self.vehicle.bounding_box.extent.y)*np.tan(np.radians(-args.lower_fov))
        lidar_transform = carla.Transform(carla.Location(z=2*self.vehicle.bounding_box.extent.z+hp))
        self.lidar= world.spawn_actor(self.get_lidar_bp(args), lidar_transform, attach_to=self.vehicle)
        self.lidar.listen(lambda data : self.lidar_callback(data))

    def get_random_blueprint(self):
        blueprints = self.world.get_blueprint_library().filter('vehicle')
        blueprints = [x for x in blueprints if int(x.get_attribute('number_of_wheels')) == 4]
        blueprints = [x for x in blueprints if not x.id.endswith(('isetta','carlacola','cybertruck','t2'))]
        return random.choice(blueprints)

    def get_lidar_bp(self, args):
        lidar_bp = self.world.get_blueprint_library().find('sensor.lidar.ray_cast')
        lidar_bp.set_attribute('dropoff_general_rate', '0.0')
        lidar_bp.set_attribute('dropoff_intensity_limit', '1.0')
        lidar_bp.set_attribute('dropoff_zero_intensity', '0.0')
        lidar_bp.set_attribute('points_per_second', str(args.points_per_cloud*args.fps))
        lidar_bp.set_attribute('rotation_frequency', str(args.fps))
        lidar_bp.set_attribute('channels', str(args.channels))
        lidar_bp.set_attribute('lower_fov', str(args.lower_fov))
        lidar_bp.set_attribute('range', str(args.range))
        return lidar_bp

    def lidar_callback(self, data):
        points = np.copy(np.frombuffer(data.raw_data, dtype=np.dtype('f4')))
        point_cloud = np.reshape(points, (int(points.shape[0] / 4), 4))
        self.sensorQueue.put((data.frame, point_cloud, data.transform))

    def destroy(self):
        self.lidar.destroy()
        self.vehicle.destroy()

class Walker:
    '''Spawns a walker in a given transform of the environment'''

    #Class variable that stores references to all instances
    instances = []

    def __init__(self, transform, world, args):
        '''Try to spawn walker in a given transform, if successful adds reference to instance list'''

        self.world = world

        #try spawning the actor
        self.walker= world.try_spawn_actor(self.get_random_blueprint(), transform)
        if self.walker is None:
            return
        Walker.instances.append(self)
        self.id = self.walker.id

        #spawn a controller for the walker
        walker_controller_bp = world.get_blueprint_library().find('controller.ai.walker')
        self.controller = world.spawn_actor(walker_controller_bp, carla.Transform(), self.walker)

    def start_controller(self):
        self.controller.start()
        self.controller.go_to_location(self.world.get_random_location_from_navigation())

    def get_random_blueprint(self):
        blueprints = self.world.get_blueprint_library().filter('walker.pedestrian.*')
        return random.choice(blueprints)

    def destroy(self):
        self.controller.stop()
        self.controller.destroy()
        self.walker.destroy()

def transformPts(transform, pts, inverse=False):
    #split intensity from 3D coordinates, add homogeneus coordinate
    intensity = pts[:,-1,np.newaxis].copy()
    pts[:,-1] = 1

    #perform transformation
    mat = transform.get_inverse_matrix() if inverse else transform.get_matrix()
    mat = np.array(mat)
    ptst = np.dot(pts, mat.T)

    #merge intensity back
    ptst = np.concatenate([ptst[:,:3],intensity], axis=1)
    return ptst

def main(args):
    try:
        #Load client & world
        client = carla.Client(args.host, args.port)
        client.set_timeout(9.0)
        world = client.load_world(args.map)

        #Set configs
        settings = world.get_settings()
        traffic_manager = client.get_trafficmanager(8000)
        traffic_manager.set_synchronous_mode(True)
        traffic_manager.set_random_device_seed(args.seed)
        settings.fixed_delta_seconds = 1. / args.fps
        settings.synchronous_mode = True
        settings.no_rendering_mode = args.no_rendering
        world.apply_settings(settings)

        #Apply seed for reproducibility
        random.seed(args.seed)

        #Spawn vehicles (select one random point and only keep the points within the range - specificed according to lidar range)
        logging.info('Spawning vehicles')
        spawn_points = [waypoint.transform for waypoint in world.get_map().generate_waypoints(5)] # waypoints every x meters 
        sp_choice = random.choice(spawn_points)
        spawn_points = [sp for sp in spawn_points if sp.location.distance(sp_choice.location) < args.range/2]
        while(len(Vehicle.instances) < args.nvehicles):
            transform = random.choice(spawn_points)
            transform.location.z += 0.3 #avoid colision with ground
            Vehicle(transform, world, args)

        #Spawn walkers in the environments
        logging.info('Spawning pedestrians')
        retries = 0
        while(len(Walker.instances) < args.npedestrians):
            retries += 1
            if retries > 1000:
                logging.error('Could not spawn pedestrians after 1000 tries. Crash.')
                exit(1)
            location = world.get_random_location_from_navigation()
            if location.distance(sp_choice.location) > args.range/2:
                continue
            Walker(carla.Transform(location=location), world, args)

        #Create HDF5 file with datasets
        compression_opts = {'compression':'gzip', 'compression_opts':9}
        if args.save != '':
            f = h5py.File(args.save, 'w')
            f.create_dataset('point_cloud', (args.frames, args.nvehicles, args.points_per_cloud, 4), dtype='float16', **compression_opts)
            f.create_dataset('lidar_pose', (args.frames, args.nvehicles, 6), dtype='float32', **compression_opts)
            f.create_dataset('vehicle_boundingbox', (args.frames, args.nvehicles, 8), dtype='float32', **compression_opts)
            f.create_dataset('pedestrian_boundingbox', (args.frames, args.npedestrians, 8), dtype='float32', **compression_opts)

        #Event loop
        savedFrames = -args.burn
        while(savedFrames < args.frames):
            world.tick()
            snap = world.get_snapshot()

            #Pedestrian controllers must be started after first tick
            if savedFrames == 1:
                for w in Walker.instances:
                    w.start_controller()
            
            try:
                for i, v in enumerate(Vehicle.instances):
                    s = v.sensorQueue.get(True,5)
                    pcl = s[1]
                    transform = s[2]

                    #if burning frames we should not save them
                    if savedFrames < 0 or args.save == '':
                        continue

                    #pad pcl with zeros to make sure it has shape [args.points_per_cloud,3]
                    pcl_pad = np.pad(pcl, ((0, args.points_per_cloud-pcl.shape[0]),(0,0)), mode='constant')

                    #get vehicle transform in the current frame and extent (extent has half the dimensions)
                    v_transform = snap.find(v.id).get_transform()
                    v_ext = v.vehicle.bounding_box.extent 

                    #write data to file
                    f['point_cloud'][savedFrames,i] = pcl_pad
                    f['lidar_pose'][savedFrames, i] = np.array([transform.location.x,transform.location.y,transform.location.z, transform.rotation.pitch,transform.rotation.yaw,transform.rotation.roll])
                    f['vehicle_boundingbox'][savedFrames, i] = np.array([v_transform.location.x,v_transform.location.y,v_transform.location.z+v_ext.z,v_transform.rotation.yaw,v_transform.rotation.pitch,2*v_ext.x,2*v_ext.y,2*v_ext.z])
                for i, w in enumerate(Walker.instances):
                    if savedFrames < 0 or args.save == '':
                        continue
                    w_transform = snap.find(w.id).get_transform()
                    w_ext = w.walker.bounding_box.extent
                    f['pedestrian_boundingbox'][savedFrames, i] = np.array([w_transform.location.x,w_transform.location.y,w_transform.location.z,w_transform.rotation.yaw,w_transform.rotation.pitch,2*w_ext.x,2*w_ext.y,2*w_ext.z])
            except Empty:
                logging.error(f'Missing sensor data for frame {snap.frame}!')
            else:
                savedFrames += 1

            if savedFrames < 0:
                logging.info(f'World frame {snap.frame} burnt, {-savedFrames} to start recording')
            else:
                logging.info(f'World frame {snap.frame} saved succesfully as frame {savedFrames}')
            time.sleep(0.01)

        logging.info(f'Finished saving {args.frames} frames!')

    finally:
        for a in Vehicle.instances + Walker.instances:
            a.destroy()
        Vehicle.instances.clear()
        Walker.instances.clear()

if __name__ == '__main__':
    logging.basicConfig(format='%(message)s', level=logging.INFO)
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        '--host',
        metavar='H',
        default='127.0.0.1',
        help='IP of the host server (default: 127.0.0.1)')
    argparser.add_argument(
        '-p', '--port',
        metavar='P',
        default=2000,
        type=int,
        help='TCP port to listen to (default: 2000)')
    argparser.add_argument(
        '-m', '--map',
        metavar='M',
        default='Town03',
        type=str,
        help='Map name (default: Town03)')
    argparser.add_argument(
        '--channels',
        default=64.0,
        type=float,
        help='lidar\'s channel count (default: 64)')
    argparser.add_argument(
        '--range',
        default=100.0,
        type=float,
        help='lidar\'s maximum range in meters (default: 100.0)')
    argparser.add_argument(
        '--lower-fov',
        default=-25.0,
        type=float,
        help='lidar\'s lower vertical fov angle in degrees (default: -25.0)')
    argparser.add_argument(
        '--points-per-cloud',
        default=50000,
        type=int,
        help='lidar\'s points per measurement (default: 50000)')
    argparser.add_argument(
        '--fps',
        default=10.0,
        type=float,
        help='frames per second, define the fixed simulation time-steps. (default: 10fps)')
    argparser.add_argument(
        '--nvehicles',
        default=0,
        type=int,
        help='number of vehicles in the environment (default: 0)')
    argparser.add_argument(
        '--npedestrians',
        default=0,
        type=int,
        help='number of pedestrians in the environment (default: 0)')
    argparser.add_argument(
        '--no-autopilot',
        action='store_false',
        help='disables the autopilot so the vehicle will remain stopped')
    argparser.add_argument(
        '--no-rendering',
        action='store_true',
        help='use the no-rendering mode which will provide some extra'
        ' performance but you will lose the articulated objects in the'
        ' lidar, such as pedestrians')
    argparser.add_argument(
        '-s', '--save',
        default='',
        type=str,
        help='Snippet path and filename with extension (h5py or h5)')
    argparser.add_argument(
        '--frames',
        default=50,
        type=int,
        help='Number of frames to save (default: 50)')
    argparser.add_argument(
        '--burn',
        default=30,
        type=int,
        help='Number of frames to discard before recording (default: 30)')
    argparser.add_argument(
        '--seed',
        default=int(time.time()),
        type=int,
        help='Random seed for reproducibility (default: time.time())')
    args = argparser.parse_args()

    try:
        main(args)
    except KeyboardInterrupt:
        pass
    finally:
        logging.info('Finished simulation')

