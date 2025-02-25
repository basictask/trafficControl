"""
This is an agent that's implemented using graph-convoltuional neural networks.
"""
# Own
from suppl import JUNCTION_TYPES, ACTION_NAMES, check_all_attributes_initialized, count_incoming_lanes, choose_random_action
from agents.agent_1_net import ReplayBuffer
# Generic
import os
import random
import numpy as np
import pandas as pd
# Deep Learning
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as fn
# Arguments
import configparser
args = configparser.ConfigParser()
args.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), '../config.ini'))


class Net(nn.Module):
    def __init__(self, state_size: int, action_size: int):
        super(Net, self).__init__()

        # Inner parameters
        self.state_size = state_size
        self.action_size = action_size
        self.n_neurons = [int(x) for x in args['learning'].get('n_neurons_local').split(',')]

        # Define the layers
        self.fc1 = nn.Linear(self.state_size, self.n_neurons[0])
        self.fc2 = nn.Linear(self.n_neurons[0], self.n_neurons[1])
        self.d1 = nn.Dropout(p=0.5)
        self.fc3 = nn.Linear(self.n_neurons[1], self.n_neurons[2])
        self.d2 = nn.Dropout(p=0.5)
        self.fc4 = nn.Linear(self.n_neurons[2], self.n_neurons[3])
        self.fc5 = nn.Linear(self.n_neurons[3], self.action_size)

        check_all_attributes_initialized(self)

    def forward(self, features: torch.tensor):
        # Compute the ending node
        features = features.float()
        x = fn.relu(self.fc1(features))
        x = fn.relu(self.fc2(x))
        x = self.d1(x)
        x = fn.relu(self.fc3(x))
        x = self.d2(x)
        x = fn.relu(self.fc4(x))
        end_q = fn.relu(self.fc5(x))
        end_q = end_q.squeeze(0)
        return end_q


class Agent:
    """
    This class defines the reinforcement learning agent
    """
    def __init__(self, state_shape: tuple, action_size: int, state_high: pd.DataFrame):
        """
        Setup the agent by reading the learning parameters from the config
        :param state_shape: Size of the state-definition matrix
        :param action_size: Number of actions that is possible to take
        :param state_high: Numpy array of the
        """
        # Reading parameters
        self.tau = args['learning'].getfloat('tau')
        self.gamma = args['learning'].getfloat('gamma')
        self.batch_size = args['learning'].getint('batch_size')
        self.buffer_size = int(args['learning'].getfloat('buffer_size'))
        self.update_every = args['learning'].getfloat('update_every')
        self.learning_rate = args['learning'].getfloat('learning_rate')
        self.embedding_size = args['learning'].getint('embedding_size')
        self.n_neurons = [int(x) for x in args['learning'].get('n_neurons_local').split(',')]
        self.trafficlight_inbound = [int(x) for x in args['trafficlight'].get('allow_inbound').split(',')]

        # Setting inner parameters
        self.error_track = []
        self.action_size = action_size
        self.state_high = state_high
        self.n_nodes = state_shape[0]
        self.state_shape = state_shape
        self.state_size = self.n_nodes * self.n_nodes
        self.current_start = torch.randint(size=(1,), low=0, high=self.n_nodes)
        self.memory = ReplayBuffer(self.buffer_size, self.batch_size)
        self.t_step = 0  # Initialize time step (for self.update_every)

        # Network, optimizer and replay memory
        self.end_nn = Net(1, self.n_nodes)
        self.end_optimizer = optim.Adam(self.end_nn.parameters(), lr=self.learning_rate)

        self.action_nn = Net(2, self.action_size)
        self.action_optimizer = optim.Adam(self.action_nn.parameters(), lr=self.learning_rate)

        self.memory = ReplayBuffer(self.buffer_size, self.batch_size)

        check_all_attributes_initialized(self)

    def act(self, state: pd.DataFrame, eps: float) -> (int, int, int, bool):
        """
        Take some action based on state
        :param state: State-definition matrix that serves as the input for the model
        :param eps: Epsilon value (probability of exploration)
        :return: None
        """
        # Turn on eval mode
        self.end_nn.eval()
        self.action_nn.eval()

        # state_tensor = torch.tensor(np.array(state), dtype=torch.float32)  # Convert the state to float and flatten

        with torch.no_grad():
            start_i = self.current_start
            end_q = self.end_nn(start_i)
            end_q[self.current_start] = float('-inf')  # Do not allow taking the same action
            end_i = torch.argmax(end_q).unsqueeze(-1)
            action_input = torch.cat((start_i, end_i), dim=-1)
            self.current_start = end_i

            action_q = self.action_nn(action_input)
            action_q = self.get_valid_actions(start_i, end_i, action_q, state)
            action_i = torch.argmax(action_q, dim=-1, keepdim=True)

        self.end_nn.train()  # Put into train mode
        self.action_nn.train()

        # Epsilon-greedy action selection
        if random.random() > eps:
            return int(start_i), int(end_i), int(action_i), False

        # Random action selection
        else:
            start = int(self.current_start)
            end = np.random.randint(self.n_nodes)
            while end == start:
                end = np.random.randint(self.n_nodes)
            self.current_start = torch.tensor(end).unsqueeze(0)
            action = choose_random_action(start, end, state, self.action_size, self.state_high, self.trafficlight_inbound)
            return start, end, action, True

    def get_current_state(self, start_i: torch.tensor, end_i: torch.tensor, state, normalize: bool) -> torch.tensor:
        """
        Find the current element in the state-representation matrix. This method is needed because the input size varies at learning and prediction
        :param start_i: Starting node tensor
        :param end_i: Ending node tensor
        :param state: State tensor
        :param normalize: The state will be divided by the corresponding state_high value
        :return: A 1-dimensional torch tensor: either 1*1 or 1*batch_size
        """
        if state.shape == self.state_shape:
            if normalize:
                current_state = torch.tensor(state.loc[int(start_i), int(end_i)] / self.state_high.loc[int(start_i), int(end_i)]).unsqueeze(-1)  # x(start, end)
            else:
                current_state = torch.tensor(state.loc[int(start_i), int(end_i)]).unsqueeze(-1)
        else:
            state = state[:, :self.state_size]  # Remove the previously appended part (unique to this model)
            state_unpacked = []
            for i in range(state.shape[0]):
                state_unpacked.append(state[i, :].reshape(self.state_shape)[start_i[i][0], end_i[i][0]])
            current_state = torch.tensor(state_unpacked).unsqueeze(-1)
        return current_state

    def get_valid_actions(self, start_i: torch.tensor, end_i: torch.tensor, action_q: torch.tensor, state) -> torch.tensor:
        """
        Inspect the context of the starting and ending node and eliminate all the invalid actions
        :param start_i: Starting node index
        :param end_i: Ending node index
        :param action_q: Torch tensor for the action values
        :param state: State-definition matrix
        :return: A new torch tensor of Q-values with invalid actions set to -inf
        """
        current_state_start_end = self.get_current_state(start_i, end_i, state, normalize=False)
        current_state_end_end = self.get_current_state(end_i, end_i, state, normalize=False)

        if action_q.dim() == 1:
            action_q = action_q.unsqueeze(0)
            n_incoming = [count_incoming_lanes(state, int(end_i))]
        else:
            n_incoming = []
            for i in range(len(start_i)):
                n_incoming.append(count_incoming_lanes(pd.DataFrame(state[i][:100].detach().reshape(self.state_shape), dtype=int), int(end_i[i])))

        for i in range(len(start_i)):
            start = int(start_i[i])
            end = int(end_i[i])
            current_junction = JUNCTION_TYPES[int(current_state_end_end[i])]

            if start != end and int(current_state_start_end[i]) == self.state_high.loc[start, end]:  # Maximum number of lanes reached
                action_q[i, ACTION_NAMES['add_lane']] = float('-inf')

            elif start != end and int(current_state_start_end[i]) == 0:
                action_q[i, ACTION_NAMES['remove_lane']] = float('-inf')

            if start == end:
                action_q[i, ACTION_NAMES['add_lane']] = float('-inf')
                action_q[i, ACTION_NAMES['remove_lane']] = float('-inf')

            if current_junction == 'righthand':
                action_q[i, ACTION_NAMES['add_righthand']] = float('-inf')

            elif current_junction == 'roundabout':
                action_q[i, ACTION_NAMES['add_roundabout']] = float('-inf')

            elif current_junction == 'trafficlight':
                action_q[i, ACTION_NAMES['add_trafficlight']] = float('-inf')

            if not n_incoming[i] in self.trafficlight_inbound:
                action_q[i, ACTION_NAMES['add_trafficlight']] = float('-inf')

        return action_q

    def step(self, state: pd.DataFrame, start: int, end: int, action: int, reward: int, next_state: pd.DataFrame, successful: bool) -> None:
        """
        Save one step in the reinforcement learning environment
        :param state: State before stepping
        :param start: Starting node for the infrastructure
        :param end: Ending node for the infrastructure
        :param action: Action that was the prediction of the model in the state
        :param reward: Reward that was received for the action
        :param next_state: Next state of the environment
        :param successful: If the operation has completed successfully
        :return: None
        """
        self.memory.add(state, start, end, action, reward, next_state, int(successful))  # Save experience in replay memory
        self.t_step = (self.t_step + 1) % self.update_every  # Update the time step

        if self.t_step == 0 and len(self.memory) > self.batch_size:  # If there's enough experience in the memory we will sample it and learn
            self.learn()

    def learn(self):
        """
        Apply the gradients based on previoyus experience
        :return: None
        """
        states, starts, ends, actions, rewards, next_states, successfuls = self.memory.sample()  # Obtain a random mini-batch

        # Make a copy of the states and convert them to state tensors
        next_states = next_states.clone().detach().view(self.batch_size, self.n_nodes, self.n_nodes).requires_grad_(True)
        states = states.clone().detach().view(self.batch_size, self.n_nodes, self.n_nodes).requires_grad_(True)

        # Estimating the Q-values for end
        local_end_q = self.end_nn(starts)
        local_end_q = local_end_q.gather(1, starts)
        target_end_q = self.end_nn(ends)
        target_end_q = target_end_q.detach().max(1)[0].unsqueeze(1)
        target_end_q = rewards + self.gamma * target_end_q

        # Stepping with the end optimizer
        end_loss = fn.mse_loss(local_end_q, target_end_q)
        self.end_optimizer.zero_grad()
        end_loss.backward()
        self.end_optimizer.step()

        # Q-values for the action
        local_action_q = self.action_nn(ends)
        local_action_q = local_action_q.gather(1, actions)
        starts, ends, target_action_q = self.action_nn(next_states, starts, ends)
        target_action_q = target_action_q.detach().max(1)[0].unsqueeze(1)
        target_action_q = rewards + self.gamma * target_action_q

        # Stepping with the action optimizer
        action_loss = fn.mse_loss(local_action_q, target_action_q)
        self.error_track.append(action_loss)
        self.action_optimizer.zero_grad()
        action_loss.backward()
        self.action_optimizer.step()

    def save_models(self):
        """
        Iterates over all the models and saves their states
        :return: None
        """
        torch.save(self.action_nn.state_dict(), './models/action_nn.pth')
        torch.save(self.end_nn.state_dict(), './models/end_nn.pth')
