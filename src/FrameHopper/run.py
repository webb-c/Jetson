import datetime
from typing import Tuple, Union, Dict
from src.FrameHopper.agent import Agent
from src.FrameHopper.environment import Environment


def testor_frameHopper(conf:Dict[str, Union[str, int, bool, float]], communicator, video_processor) -> bool:
    env = Environment(conf, communicator, video_processor, run=True)
    agent = Agent(conf, run=True)
    done = False
    s = env.reset()
    a_list = []
    
    step = 0
    print("Ready ...")
    
    while not done:
        a = agent.get_action(s, False)
        a_list.append(a)
        trans, done = env.step(a)
        if done:
            break
        _, _, s, _ = trans
        step += 1
        if conf['debug_mode']:
            print(s, a, step)
    
    if conf['jetson_mode']:
        env.communicator.get_message()
        env.communicator.send_message("finish")
        env.communicator.close_queue()
        
    finish_time = datetime.datetime.now().strftime("%y%m%d-%H%M%S")
    
    fraction_value = env.video_processor.num_processed / env.video_processor.num_all 
    rounded_fraction = round(fraction_value, 4)
    
    conf['idx_list'] = trans
    conf['fraction'] = rounded_fraction
    
    return True, finish_time