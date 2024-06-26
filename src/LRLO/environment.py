"""
represents the environment in which the model interacts; 
it uses replay buffers because we using offline-learning.
"""
import collections
import random
import os
import math
import numpy as np
from typing import Tuple, List, Union, Dict
from src.LRLO.util.get_state import cluster_pred, cluster_load, cluster_init
from src.LRLO.util.cal_quality import get_FFT, get_MSE
from src.LRLO.util.cal_F1 import get_F1_with_idx

ARRIVAL_MAX = 1.0

random.seed(42)


class ReplayBuffer():
    """Q learning 학습을 위해 transition을 저장하는 버퍼
    """
    def __init__(self, buffer_size:int):
        """init
        
        Args:
            buffer_size (int): 버퍼에 저장가능한 총 transition 개수
        """
        self.buffer = collections.deque(maxlen=buffer_size)
    
    
    def put_data(self, trans:Tuple[Union[int, Tuple[float, float]], int, float, Union[int, Tuple[float, float]], float, bool, int]):
        """인자로 전달된 transition을 버퍼에 넣는다.
        
        Args:
            trans (Tuple[state, action, reward, state_prime, action_prob, done, guide])
        """
        self.buffer.append(trans)
    
    
    def get_data(self):
        """버퍼에 저장된 데이터를 랜덤하게 하나 꺼낸다.
        
        Returns:
            trans (Tuple[state, action, reward, state_prime, action_prob, done, guide])
        """
        trans = random.choice(self.buffer)  # batch size = 1
        return trans
    
    
    def get_size(self) -> int:
        """현재 버퍼에 쌓인 데이터의 개수를 반환한다.
        
        Returns:
            int: length of buffer
        """
        return len(self.buffer)


class Environment():
    """강화학습 agent와 상호작용하는 environment
    """
    def __init__(self, conf:Dict[str, Union[str, bool, int, float]], communicator, video_processor, run:bool=False, jetson_mode:bool=False):
        """init

        Args:
            conf (Dict[str, Union[bool, int, float]]): train/test setting
            run (bool, optional): testing을 수행하는가?
        
        Calls:
            self.__detect(): obj, fft load
            utils.get_state.cluster_init(): cluster 초기화
            utils.get_state.cluster_load(): cluster_path에서 불러옴
            self.reset(): Env reset
        """
        self.run = run
        self.jetson_mode = jetson_mode
        
        self.fps = conf['fps']
        self.V = conf['V']
        self.debug_mode = conf['debug_mode']
        self.action_dim = conf['action_dim']
        self.radius = conf['radius']
        
        self.communicator = communicator
        self.video_processor = video_processor
        self.jetson_mode = conf['jetson_mode']
        
        self.frame_shape = self.video_processor.frame_shape
        self.model = cluster_init(conf['state_num'])
        print("load cluster model in init...")
        self.model = cluster_load(conf['cluster_path'])
        
        if not self.run:
            # training (reward) argument
            self.threshold = conf['threshold']
            self.reward_method = conf['reward_method']
            self.important_method = conf['important_method']
            self.beta = conf['beta']
            self.window = conf['window']
            self.buffer = ReplayBuffer(conf['buffer_size'])
            self.__detect(conf['detection_path'], conf['FFT_path'])

        self.reset()

    
    def reset(self) -> Union[int, List[float]]:
        """Env reset: Arrival data, reward, idx, frame_reader, state 초기화 

        Args:
            show_log (bool, optional): command line에 이번 에피소드 전체의 transition을 출력할 것인가?

        Returns:
            Union[int, List[float, float]]: initial state를 반환한다.
            
        Calls:
            utils.cal_quality.get_FFT(): run 모드일 때 FFT 계산을 위해 사용한다.
            utils.get_state.cluster_pred: train, Q-learning에서 state를 구하기 위해 사용한다.
        """
        self.reward_sum, self.sum_a = 0, 0
        if self.jetson_mode:
            self.sum_A = 0
            self.prev_A, self.target_A = self.action_dim, self.action_dim

        self.cur_frame, self.last_skip_frame, _, self.idx = self.video_processor.get_frame()
        self.state = self.__observe_environment()
        
        self.trans_list = []

        return self.state

    
    def __observe_environment(self):
        self.origin_state = [get_MSE(self.last_skip_frame, self.cur_frame), get_FFT(self.cur_frame, self.radius)]
        state = cluster_pred(self.origin_state, self.model)
        
        return state


    def __detect(self, detection_path: str, FFT_path: str):
        """학습에 사용하는 영상에 해당되는 사전처리된 데이터 로드

        Args:
            detection_path (str): 검증된 detection_path 경로
            FFT_path (str): 검증된 cluster_path
        """
        self.FFT_list = np.load(FFT_path)
        label_list =  os.listdir(detection_path)
        self.obj_num_list = []

        for label in label_list :
            label_path = detection_path+"/"+label
            with open(label_path, 'r') as file :
                lines = file.readlines()
                self.obj_num_list.append(len(lines))


    def step(self, action:int) -> Tuple[Union[int, List[float]], float, bool]:
        """인자로 전달된 action을 수행하고, 행동에 대한 reward와 함께 next_state를 반환합니다..

        Args:
            action (int): 수행할 action [0, fps]

        Returns:
            Tuple[next_state, reward, done]
        
        Calls:
            self.Communicator.get/sent_omnet_message: omnet 통신
            __triggered_by_guideL Lyapunov based guide가 새로 들어왔을 때 변수 갱신, 리워드 계산을 위해 호출
        """
        #print("\naction:", action)
        new_A = -1
        
        for a in range(self.action_dim+1):
            if a < action :
                # self.skipped_frame
                ret = self.video_processor.read_video(skip=True)
                if not ret:
                    if self.run:
                        idx_list = self.video_processor.processed_frames_index
                        return idx_list, 0, True
                    return self.state, 0, True
                
            elif a >= action : 
                # processed frame
                if self.jetson_mode:
                    self.communicator.get_message()
                    self.communicator.send_message("action")
                    self.communicator.get_message()
                    self.communicator.send_message(self.frame_shape)
                    
                ret = self.video_processor.read_video(skip=False)
                if not ret:
                    if self.run:
                        idx_list = self.video_processor.processed_frames_index
                        return idx_list, 0, True
                    return self.state, 0, True
        
        if self.jetson_mode:
            self.communicator.get_message()
            self.communicator.send_message("reward") 
            path_cost = float(self.communicator.get_message())
            self.communicator.send_message("ACK")
        
            ratio_A = ARRIVAL_MAX if path_cost == 0 else min(ARRIVAL_MAX, self.V / path_cost)
            new_A = math.floor(ratio_A*(self.action_dim))
            
            if self.debug_mode:
                print("path cost:", path_cost)
                print("scaling cost using V (V/path_cost):", ratio_A)
                print("arrival rate using fps:", new_A)
    
        self.cur_frame, self.last_skip_frame, _, self.idx = self.video_processor.get_frame()
        r = self.__triggered_by_guide(new_A, action)
        
        return self.state, r, False


    def __triggered_by_guide(self, new_A:int, action:int) -> float:
        """Lyapunov based guide가 전달되었을 때, 갱신하고 reward를 계산합니다.

        Args:
            new_A (int): 새로운 lyapunov based guide
            action (int): 수행하고자 했던 action

        Returns:
            float: reward
        
        Calls:
            utils.cal_quality.get_MsE/get_FFT: state 계산을 위해 호출 (origin)
            utils.get_state.cluster_pred: state 계산을 위해 호출 (int)
            self.__reward_function: reward 계산
            
        """
        r = 0
        if self.jetson_mode:
            self.sum_A += self.target_A
            self.trans_list.append(self.action_dim-self.target_A)
            self.prev_A = self.target_A
            self.target_A = new_A
        self.send_A = self.action_dim - action
        self.sum_a += self.send_A
        self.trans_list.append(self.state)
        self.trans_list.append(action)
        
        self.state = self.__observe_environment()
        
        self.trans_list.append(self.state)
        
        if not self.run :
            r = self.__reward_function()
            self.buffer.put_data(self.trans_list)
        self.trans_list = []
        
        return r

    def __cal_important(self) -> List[float]:
        """현재 idx에서 1초동안의 프레임 (=fps)을 이용하여 물체 개수로부터 중요도를 계산합니다.
        # @param
        Returns: List[important_score]
        """
        def max_func(lst):
            if len(lst) == 0:
                return 0
            return max(lst)
        
        def min_func(lst):
            if len(lst) == 0:
                return 0
            return min(lst)
        
        def avg_func(lst):
            if len(lst) == 0:
                return 0
            return sum(lst) / len(lst)
        
        def identity_func(lst):
            return 1
        
        std_idx = self.idx - self.action_dim 

        ## [2]: criteria: '0' -> object_num / '1' -> 1 - F1 score 
        score_list=[]
        if self.important_method[2] == '0':
            if self.important_method[0] == '0':
                score_list = self.obj_num_list[ std_idx : std_idx+self.window+1 ]
            
            elif self.important_method[0] == '1':
                gap = int((self.window - 1)//2)
                score_list = self.obj_num_list[ std_idx-gap : std_idx+self.action_dim ]
            
        if self.important_method[2] == '1':
            last_idx = self.idx - self.action_dim-1
            if last_idx < 1:
                last_idx = 1
            for f in range(self.action_dim) :
                cur_idx = std_idx + f
                if cur_idx < 1:
                    cur_idx = 1
                f1 = get_F1_with_idx(last_idx, cur_idx, self.video_processor.video_path)
                score_list.append(1 - f1)
            
        ## [1]: regularization method: *'3' only using in i[2] == '1'
        reg_list = []
        if self.important_method[1] == '0':
            reg_func = max_func
            
        elif self.important_method[1] == '1':
            reg_func = min_func
            
        elif self.important_method[1] == '2':
            reg_func = avg_func
        
        elif self.important_method[1] == '3':
            reg_func = identity_func
        
        ## [0]: relative important calculate bound: *'1' only using in i[2] == '0'
        if self.important_method[0] == '0':
            for f in range(self.action_dim) :
                reg_list.append(reg_func(score_list))
        
        elif self.important_method[0] == '1':
            gap = int((self.window - 1)//2)
            for f in range(self.action_dim) :
                cur_idx = gap + f
                reg_list.append(reg_func(score_list[ cur_idx-gap : cur_idx+gap+1 ]))
        
        important_list = []
        for f in range(self.action_dim) :
            importance = (score_list[f] / reg_list[f]) if reg_list[f] != 0 else 0
            important_list.append(importance)
        
        if self.debug_mode and self.important_print_count < 10:
            self.important_print_count += 1
            print("score:", score_list, len(score_list))
            print("reg:",  reg_list, len(reg_list))
            print("important:", important_list, len(important_list))
            print()
        
        return important_list


    def __reward_function(self):
        """reward를 계산합니다.
        # @param
        Returns:
            float: reward
        Calls:
            self.__cal_important: 리워드 계산을 위해 각 프레임의 중요도를 계산합니다. 
        
        # NOTE: about reward_function
        supported
        - 00: default
        - 10: important threshold
        - 11: important threshold + plus reward for skip length
        - 20: F1 threshold
        - 21: F1 threshold + plus reward for skip length
        """
        important_list = self.__cal_important()
        if self.jetson_mode:
            _, s, a, s_prime = self.trans_list
        else:
            s, a, s_prime = self.trans_list
        
        plus_beta = 1 - self.beta
        minus_beta = self.beta
        
        ## [1]: plus reward for skip length use: *'1' not used in r[1] == '0'
        if self.reward_method[1] == '0':
            skip_weight = 1
            
        elif self.reward_method[1] == '1':
            skip_weight = a
        
        plus_div = len(important_list[a+1:])
        minus_div = len(important_list[:a+1]) 
        if plus_div == 0: plus_div = 1
        if  minus_div == 0: minus_div = 1
        
        ## [0]: reward method
        if self.reward_method[0] == '0':
            r = (plus_beta*sum(important_list[a+1:])/plus_div) - (minus_beta*sum(important_list[:a+1])/minus_div) 
            if self.debug_mode and self.reward_print_count < 10:
                self.reward_print_count  += 1
                print("method 0:", r)
                print()
            
        elif self.reward_method[0] == '1':
            score_sum = sum(important_list[a+1:])/plus_div - sum(important_list[:a+1])/minus_div
            # score_sum = plus_beta*sum(important_list[a+1:]) - minus_beta*sum(important_list[:a+1])
            # score_sum = sum(important_list)
            if score_sum < self.threshold:
                if score_sum < 0:
                    r = minus_beta * sum(important_list[:a+1])/minus_div
                else:
                    r = -1 * minus_beta * sum(important_list[:a+1])/minus_div
            else :
                if score_sum < 0:
                    r = -1 * skip_weight * plus_beta * sum(important_list[a+1:])/plus_div
                else:
                    r = skip_weight * plus_beta * sum(important_list[a+1:])/plus_div
            
            if self.debug_mode and self.reward_print_count < 10:
                self.reward_print_count += 1
                print("method 1:", r)
                print("Important score status", score_sum)
                print()
            
        self.trans_list.append(r)
        self.reward_sum += r
        
        if self.show_log :
            if self.jetson_mode:
                self.logList.append("s(t): {:2d}\tu(t): {:2d}\ts(t+1): {:2d}\tr(t): {:.5f}\nA(t): {:2d}\tA(t+1): {:2d}".format(s[0], a, s_prime[0], r, self.prev_A, self.target_A))
            else:
                self.logList.append("s(t): {:2d}\tu(t): {:2d}\ts(t+1): {:2d}\tr(t): {:.5f}".format(s[0], a, s_prime[0], r))
            
            
        return r


    def show_trans(self) :
        """show_log가 true일 때, 에피소드 전체에서 각각의 transition을 모두 출력합니다.
        """
        for row in self.logList :
            print(row)
        return

