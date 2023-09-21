"""
Learning neural ODEs with exogenous inputs from time series data
"""

import torch
from neuromancer.psl import plot
from neuromancer import psl
import matplotlib.pyplot as plt
from torch.utils.data import DataLoader

from neuromancer.system import Node, System
from neuromancer.dynamics import integrators, ode
from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
from neuromancer.loggers import BasicLogger
from neuromancer.dataset import DictDataset
from neuromancer.constraint import variable
from neuromancer.loss import PenaltyLoss
from neuromancer. modules import blocks


def get_data(sys, nsim, nsteps, ts, bs):
    """
    :param nsteps: (int) Number of timesteps for each batch of training data
    :param sys: (psl.system)
    :param ts: (float) step size
    :param bs: (int) batch size

    """
    train_sim, dev_sim, test_sim = [sys.simulate(nsim=nsim, ts=ts) for i in range(3)]
    nx = sys.nx
    nu = sys.nu
    nbatch = nsim//nsteps
    length = (nsim//nsteps) * nsteps

    mean_x = modelSystem.stats['X']['mean']
    std_x = modelSystem.stats['X']['std']
    mean_u = modelSystem.stats['U']['mean']
    std_u = modelSystem.stats['U']['std']
    def normalize(x, mean, std):
        return (x - mean) / std

    trainX = normalize(train_sim['X'][:length], mean_x, std_x)
    trainX = trainX.reshape(nbatch, nsteps, nx)
    trainX = torch.tensor(trainX, dtype=torch.float32)
    trainU = normalize(train_sim['U'][:length], mean_u, std_u)
    trainU = trainU.reshape(nbatch, nsteps, nu)
    trainU = torch.tensor(trainU, dtype=torch.float32)
    train_data = DictDataset({'X': trainX, 'xn': trainX[:, 0:1, :],
                              'U': trainU}, name='train')
    train_loader = DataLoader(train_data, batch_size=bs,
                              collate_fn=train_data.collate_fn, shuffle=True)

    devX = normalize(dev_sim['X'][:length], mean_x, std_x)
    devX = devX.reshape(nbatch, nsteps, nx)
    devX = torch.tensor(devX, dtype=torch.float32)
    devU = normalize(dev_sim['U'][:length], mean_u, std_u)
    devU = devU[:length].reshape(nbatch, nsteps, nu)
    devU = torch.tensor(devU, dtype=torch.float32)
    dev_data = DictDataset({'X': devX, 'xn': devX[:, 0:1, :],
                            'U': devU}, name='dev')
    dev_loader = DataLoader(dev_data, batch_size=bs,
                            collate_fn=dev_data.collate_fn, shuffle=True)

    testX = normalize(test_sim['X'][:length], mean_x, std_x)
    testX = testX.reshape(1, nbatch*nsteps, nx)
    testX = torch.tensor(testX, dtype=torch.float32)
    testU = normalize(test_sim['U'][:length], mean_u, std_u)
    testU = testU.reshape(1, nbatch*nsteps, nu)
    testU = torch.tensor(testU, dtype=torch.float32)
    test_data = {'X': testX, 'xn': testX[:, 0:1, :],
                 'U': testU}

    return train_loader, dev_loader, test_data


if __name__ == '__main__':
    torch.manual_seed(0)

    # select system:
    #   TwoTank, CSTR, SwingEquation,
    #   VanDerPolControl, IverSimple
    #   SEIR_population, LorenzControl

    # %%  ground truth system
    system = psl.systems['CSTR']
    modelSystem = system()
    ts = modelSystem.ts
    nx = modelSystem.nx
    nu = modelSystem.nu
    raw = modelSystem.simulate(nsim=1000, ts=ts)
    plot.pltOL(Y=raw['Y'], U=raw['U'])
    plot.pltPhase(X=raw['Y'])

    # get datasets
    nsim = 2000
    nsteps = 10
    bs = 100
    train_loader, dev_loader, test_data = \
        get_data(modelSystem, nsim, nsteps, ts, bs)

    # construct NODE model in Neuromancer
    fx = blocks.MLP(nx+nu, nx, bias=True,
                     linear_map=torch.nn.Linear,
                     nonlin=torch.nn.ReLU,
                     hsizes=[80, 80, 80])
    fxRK4 = integrators.RK4(fx, h=ts)
    model = Node(fxRK4, ['xn', 'U'], ['xn'], name='NODE')
    dynamics_model = System([model], name='system')

    # %% Constraints + losses:
    x = variable("X")
    xhat = variable('xn')[:, :-1, :]

    # trajectory tracking loss
    reference_loss = 5.*(xhat == x)^2
    reference_loss.name = "ref_loss"

    # one step tracking loss
    onestep_loss = 1.*(xhat[:, 1, :] == x[:, 1, :])^2
    onestep_loss.name = "onestep_loss"

    # %%
    objectives = [reference_loss, onestep_loss]
    constraints = []
    # create constrained optimization loss
    loss = PenaltyLoss(objectives, constraints)
    # construct constrained optimization problem
    problem = Problem([dynamics_model], loss)
    # plot computational graph
    problem.show()

    # %%
    optimizer = torch.optim.Adam(problem.parameters(),
                                 lr=0.003)
    logger = BasicLogger(args=None, savedir='test', verbosity=1,
                         stdout=['dev_loss', 'train_loss'])

    trainer = Trainer(
        problem,
        train_loader,
        dev_loader,
        test_data,
        optimizer,
        patience=100,
        warmup=100,
        epochs=1000,
        eval_metric="dev_loss",
        train_metric="train_loss",
        dev_metric="dev_loss",
        test_metric="dev_loss",
        logger=logger,
    )
    # %%
    best_model = trainer.train()
    problem.load_state_dict(best_model)
    # %%

    # Test set results
    test_outputs = dynamics_model(test_data)

    pred_traj = test_outputs['xn'][:, :-1, :]
    true_traj = test_data['X']
    pred_traj = pred_traj.detach().numpy().reshape(-1, nx)
    true_traj = true_traj.detach().numpy().reshape(-1, nx)
    pred_traj, true_traj = pred_traj.transpose(1, 0), true_traj.transpose(1, 0)

    figsize = 25
    fig, ax = plt.subplots(nx, figsize=(figsize, figsize))
    labels = [f'$y_{k}$' for k in range(len(true_traj))]
    for row, (t1, t2, label) in enumerate(zip(true_traj, pred_traj, labels)):
        if nx > 1:
            axe = ax[row]
        else:
            axe = ax
        axe.set_ylabel(label, rotation=0, labelpad=20, fontsize=figsize)
        axe.plot(t1, 'c', linewidth=4.0, label='True')
        axe.plot(t2, 'm--', linewidth=4.0, label='Pred')
        axe.tick_params(labelbottom=False, labelsize=figsize)
    axe.tick_params(labelbottom=True, labelsize=figsize)
    axe.legend(fontsize=figsize)
    axe.set_xlabel('$time$', fontsize=figsize)
    plt.tight_layout()
