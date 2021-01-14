import logging
import time
import threading
import edgeiq
import sys

import pandas as pd
import numpy as np
from influxdb import InfluxDBClient
from influxdb import DataFrameClient
from datetime import datetime
from queue import Queue
from threading import Thread
import environ
import os

"""
Use pose estimation to determine human poses in realtime. Human Pose returns
a list of key points indicating joints that are sent to InfluxDB as dataeframes in the format:

Time    |   Person 0    |   Person 1    |   ...     |   Person N
datetime|   Pose obj    |   Pose obj    |   ...     |   Pose obj

To lookup the content/composition of the Pose class, visit:
https://alwaysai.co/docs/edgeiq_api/pose_estimation.html?module-edgeiq.human_pose.human_pose

To change the engine and accelerator, follow this guide:
https://alwaysai.co/docs/application_development/changing_the_engine_and_accelerator.html

To install app dependencies in the runtime container, list them in the
requirements.txt file.
"""

"""
The function handeling pose detectiom from a video stream.
It contains initialization of pose estimation engine, accelerator, and ML model.
Also setting up a browser-based streamer at localhost:5000.

The "out_q" and "break_q" arguments are used for thread safety. "out_q" contains the data 
to send to InfluxDB. "break_q" breaks the detectionloop (allowing the "record_data"-thread 
to exit) if a keyboard interrupt is detected in the main thread. 
"""
def record_data(out_q, break_q):
    pose_estimator = edgeiq.PoseEstimation("alwaysai/human-pose")
    pose_estimator.load(engine=edgeiq.Engine.DNN_OPENVINO)
    
    print("Loaded model:\n{}\n".format(pose_estimator.model_id))
    print("Engine: {}".format(pose_estimator.engine))
    print("Accelerator: {}\n".format(pose_estimator.accelerator))

    fps = edgeiq.FPS()

    try:
        with edgeiq.WebcamVideoStream(cam=0) as video_stream, \
                edgeiq.Streamer() as streamer:
            # Allow Webcam to warm up
            time.sleep(2.0)
            # Check if the Intel Compute Stick is connected and set up properly...
            try:
                pose_estimator.estimate(video_stream.read())
            except:
                pose_estimator.load(engine=edgeiq.Engine.DNN)    # ...if not, standard engine & GPU accelerator is used
                print("\nCouldn't initialize NCS. Did you connect the compute stick?")
                print("Engine: {}".format(pose_estimator.engine))
                print("Accelerator: {}\n".format(pose_estimator.accelerator))

            fps.start()
            

            # loop pose detection
            while True:

                frame = video_stream.read()
                results = pose_estimator.estimate(frame)
                
                # Generate text to display on streamer
                text = ["Model: {}".format(pose_estimator.model_id)]
                text.append(
                        "Inference time: {:1.3f} s".format(results.duration) + 
                        "\nFPS: {:.2f}".format(fps.compute_fps()))

                pose_out = dict()
                for ind, pose in enumerate(results.poses):
                    # filter out low quality results by checking confidence score
                    if pose.score > 20:                     
                        pose_out["Person {}".format(ind)] = pose
                # put result in out queue -> used by the print_data function/thread
                out_q.put(pose_out)

                streamer.send_data(results.draw_poses(frame), text)
                fps.update()

                if streamer.check_exit():
                    break
                # break_q is used to check for keyboard interrupt in the main thread
                if break_q.qsize() > 0:
                    break_q.put(1)
                    break

    finally:
        fps.stop()
        print("elapsed time: {:.2f}".format(fps.get_elapsed_seconds()))
        print("approx. FPS: {:.2f}".format(fps.compute_fps()))
        print("Streamer stopped")
        break_q.put(1)

"""
A function for sending pose data to an InfluxDB instance. 
"""
def print_data(in_q, break_q):
    # Setup Django environment 
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = environ.Env()
    env_path = os.path.join(BASE_DIR, '.env')
    environ.Env.read_env('.env')

    # Create InfluxDb client
    DB_HOST = env('DB_HOST')
    DB_USER = env('DB_USER')
    DB_PASSWORD = env('DB_PASSWORD')

    client = DataFrameClient(
        host=DB_HOST, 
        port='8086', 
        username=DB_USER, 
        password=DB_PASSWORD, 
        database='mmbox')
    
    # listening for data to send
    while True:
        # check if keyboard interrupt has been detected in the main thread
        if break_q.qsize() > 0:
            break_q.put(1)
            break
        
        time.sleep(5)
        poses = in_q.get()      # pop the data that's been put on "out_q" in the record_data function
         
        if len(poses) > 0:             # Check if people are detected (and if the pose detection has started)
                
            timestamp = time.time()
            dtidx = pd.DatetimeIndex(data=[pd.to_datetime(timestamp, unit='s', origin='unix')], name='Time')
            df = pd.DataFrame(data=poses, index=dtidx, columns=poses)

            # Alternative way to get unix timestamp
            #times = [time.time()]
            #df = pd.DataFrame(data=output, index=pd.to_datetime(times, unit='s', origin='unix'), columns=output)
            
            try: 
                client.write_points(df, 'mmbox_video_pose', batch_size=1000)    # send dataframe to InfluxDB
            except:
                print("Error writing to InfluxDB")
                
            print(df)
       
    print("Printer stopped")

"""
Starts two threads, one for detecting pose data (record_data), and one for sending data to InfluxDB (print_data)
"""
def main():

    data_q = Queue()    # Used to send data between the record/print-threads 
    break_q = Queue()   # Used to detect keyboard interrupt in t1 and t2
    t1 = threading.Thread(target=record_data, args=(data_q, break_q))
    t2 = threading.Thread(target=print_data, args=(data_q, break_q))

    t1.start()
    try:
        time.sleep(3) # wait for camera to start up
        t2.start()

        while True:
            time.sleep(.1)
    except KeyboardInterrupt:       # The only way to stop the application gracefully (well well)
        print("\n")
        break_q.put(1)
    finally:
        print("Attemting to close threads...")
        if t2.is_alive():
            t2.join()
        if t1.is_alive():
            t1.join()
        print("Threads successfully closed")


if __name__ == "__main__":
    main()
