#!/usr/bin/env python
import numpy as np
import matplotlib.pyplot as plt
import datetime as dt
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import csv
import os.path
import pandas as pd

import yfinance as yf
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability import distributions as tfd

def load_data(tickers: list):
    """
    Load relevant information from provided tickers.

    Parameters
    ----------
    tickers: list
    Stock symbols

    Returns
    -------
    Dictionary including:
    - tickers: list of symbols with available information;
    - dates: dates corresponding to available prices;
    - sectors: list of sectors at stock-level;
    - industries: list of industries at stock-level;
    - logp: log-prices at stock-level;
    - volume: volume of transitions at stock-level.
    """
    # make tickers unique
    tickers = list(set(tickers))
    # download all last year available adjusted closing prices and volumes
    dfp = yf.download(tickers, period="1y")[["Adj Close", "Volume"]]
    dfp, dfv = dfp["Adj Close"], dfp["Volume"]
    # fix inconsistency if only one stock is loaded
    if dfp.ndim == 1:
        dfp = pd.DataFrame(dfp).rename(columns={"Adj Close": tickers[0]})
        dfv = pd.DataFrame(dfv).rename(columns={"Volume": tickers[0]})
    # drop stocks that have at least two NaNs
    rm_idx = np.union1d(np.where((dfp.isnull().sum(0) > 2) == True)[0],
    np.where((dfv.isnull().sum(0) > 2) == True)[0])
    dfp.drop(columns=dfp.columns[rm_idx], inplace=True)
    dfv.drop(columns=dfv.columns[rm_idx], inplace=True)
    # raise exception if no stock is left
    if dfp.size == 0:
        raise Exception("No symbol with full information is available.")
    # propagate data backwards to fill NaNs, then forward, then drop possible duplicated dates
    dfp = dfp.fillna(method='bfill').fillna(method='ffill').drop_duplicates()
    dfv = dfv.fillna(method='bfill').fillna(method='ffill').drop_duplicates()
    # print out unavailable symbols
    missing_tickers = [tick for tick in tickers if tick not in dfp.columns]
    if len(missing_tickers) > 0:
        print('\nRemoving {} from list of symbols because yfinance could not provide full information.'.format(missing_tickers))
    # reset list of tickers and stocks
    tickers = list(dfp.columns)
    stocks = [yf.Tickers(tickers[i*254:(i+1)*254]).tickers for i in range(int(np.ceil(len(tickers) / 254)))]
    # store log-prices and volumes
    logp = np.log(dfp.to_numpy().T)
    volume = dfv.to_numpy().T

    filename = "stock_info.csv"
    print('\nAccessing stock information. For all symbols that you download for the first time, this can take a '
    'while. Otherwise, stock information is cached into ' + filename + ' and accessing it will be fast.')

    if not os.path.exists(filename):
        # create a .csv to store stock information
        with open(filename, 'w') as file:
            wr = csv.writer(file)
            for row in zip(["SYMBOL"], ["SECTOR"], ["INDUSTRY"]):
                wr.writerow(row)
    # load stock information file
    stock_info = pd.read_csv(filename)

    # load sector and industry information. If any is already available in the stock information file, load it from
    # there. Otherwise, try out if it is available in the data. If not, give it a unique name.
    sectors = []
    industries = []
    missing_sector = {}
    missing_industry = {}
    for i in range(len(tickers)):
        idx = np.where(stock_info["SYMBOL"].values == tickers[i])[0]
        if len(idx) > 0:
            sectors.append(stock_info["SECTOR"][idx[0]])
            industries.append(stock_info["INDUSTRY"][idx[0]])
        else:
            try:
                info = stocks[i // 254][i % 254].info
                sectors.append(info["sector"])
                missing_sector[tickers[i]] = sectors[-1]
                industries.append(info["industry"])
                missing_industry[tickers[i]] = industries[-1]
            except:
                sectors.append("NA_sector" + str(i))
                industries.append("NA_industry" + str(i))

    # cache information that was not present before, except for names that were given artificially.
    stock_info = zip(list(missing_sector.keys()), list(missing_sector.values()), list(missing_industry.values()))
    with open(filename, 'a+', newline='') as file:
        wr = csv.writer(file)
        for row in stock_info:
            wr.writerow(row)

    return dict(tickers=tickers, stock_dates=pd.to_datetime(dfp.index).date, volume_dates=pd.to_datetime(dfv.index).date,
                sectors=sectors, industries=industries, logp=logp, volume=volume)

def define_model(info: dict, level: str = "stock"):
    """
    Define and return graphical model.

    Parameters
    ----------
    info: dict
        Data information.
    level: str
        Level of the model; possible candidates are "stock", "industry", "sector" and "market".
    """
    tt = info['tt']
    order_scale = info['order_scale']
    order =  len(order_scale) - 1
    num_sectors = info['num_sectors']
    sec2ind_id = info['sector_industries_id']
    ind_id = info['industries_id']

    available_levels = ["market", "sector", "industry", "stock"]
    if level not in available_levels:
        raise Exception("Selected level is unknown. Please provide one of the following levels: {}.".format(available_levels))

    m = [tfd.Normal(loc=tf.zeros([1, order + 1]), scale=4 * order_scale), # phi_m
         tfd.Normal(loc=0, scale=4)] # psi_m

    if level != "market":
        m += [lambda psi_m, phi_m: tfd.Normal(loc=tf.repeat(phi_m, num_sectors, axis=0), scale=2 * order_scale), # phi_s
              lambda phi_s, psi_m: tfd.Normal(loc=psi_m, scale=2 * tf.ones([num_sectors, 1]))] # psi_s

        if level != "sector":
            sec2ind_id = info['sector_industries_id']
            m += [lambda psi_s, phi_s: tfd.Normal(loc=tf.gather(phi_s, sec2ind_id, axis=0), scale=order_scale), # phi_i
                  lambda phi_i, psi_s: tfd.Normal(loc=tf.gather(psi_s, sec2ind_id, axis=0), scale=1)] # psi_ii

            if level != "industry":
                ind_id = info['industries_id']
                m += [lambda psi_i, phi_i: tfd.Normal(loc=tf.gather(phi_i, ind_id, axis=0), scale=0.5 * order_scale), # phi
                      lambda phi, psi_i: tfd.Normal(loc=tf.gather(psi_i, ind_id, axis=0), scale=0.5)]  # psi

    if level == "market":
        m += [lambda psi_m, phi_m: tfd.Normal(loc=tf.tensordot(phi_m, tt, axes=1), scale=tf.math.softplus(psi_m))] # y
    if level == "sector":
        m += [lambda psi_s, phi_s: tfd.Normal(loc=tf.tensordot(phi_s, tt, axes=1), scale=tf.math.softplus(psi_s))] # y
    if level == "industry":
        m += [lambda psi_i, phi_i: tfd.Normal(loc=tf.tensordot(phi_i, tt, axes=1), scale=tf.math.softplus(psi_i))] # y
    if level == "stock":
        m += [lambda psi, phi: tfd.Normal(loc=tf.tensordot(phi, tt, axes=1), scale=tf.math.softplus(psi))] # y

    return tfd.JointDistributionSequentialAutoBatched(m)

def training(logp: np.array, info: dict, learning_rate: float = 0.01, num_steps: int = 20000, plot_losses: bool = False):
    """
    It performs sequential optimization over the model parameters via Adam optimizer, training at different levels to
    provide sensible initial solutions at finer levels.

    Parameters
    ----------
    logp: np.array
        Log-price at stock-level.
    info: dict
        Data information.
    learning_rate: float
        Adam's fixed learning rate.
    num_steps: int
        Adam's fixed number of iterations.
    plot_losses: bool
        If True, a losses decay plot is saved in the current directory.

    Returns
    -------
    It returns trained parameters.
    """
    optimizer = tf.optimizers.Adam(learning_rate=learning_rate)
    num_steps_l = int(np.ceil(num_steps // 4))

    # market
    model = define_model(info, "market")
    phi_m, psi_m = (tf.Variable(tf.zeros_like(model.sample()[:2][i])) for i in range(2))
    loss_m = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, logp.mean(0, keepdims=1)]),
                             optimizer=optimizer, num_steps=num_steps_l)
    # sector
    model = define_model(info, "sector")
    phi_m, psi_m = tf.constant(phi_m), tf.constant(psi_m)
    phi_s, psi_s = (tf.Variable(tf.zeros_like(model.sample()[2:4][i])) for i in range(2))
    logp_s = np.array([logp[np.where(np.array(info['sectors_id']) == k)[0]].mean(0) for k in range(info['num_sectors'])])
    loss_s = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, logp_s]),
                             optimizer=optimizer, num_steps=num_steps_l)

    # industry
    model = define_model(info, "industry")
    phi_s, psi_s = tf.constant(phi_s), tf.constant(psi_s)
    phi_i, psi_i = (tf.Variable(tf.zeros_like(model.sample()[4:6][i])) for i in range(2))
    logp_i = np.array([logp[np.where(np.array(info['industries_id']) == k)[0]].mean(0) for k in range(info['num_industries'])])
    loss_i = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, logp_i]),
                             optimizer=optimizer, num_steps=num_steps_l)
    # stock
    model = define_model(info, "stock")
    phi_i, psi_i = tf.constant(phi_i), tf.constant(psi_i)
    phi, psi = (tf.Variable(tf.zeros_like(model.sample()[6:8][i])) for i in range(2))
    loss = tfp.math.minimize(lambda: -model.log_prob([phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi, logp]),
                             optimizer=optimizer, num_steps=num_steps_l)

    if plot_losses:
        fig_name = 'losses_decay.png'
        fig = plt.figure(figsize=(20, 3))
        plt.subplot(141)
        plt.title("market-level", fontsize=12)
        plt.plot(loss_m)
        plt.subplot(142)
        plt.title("sector-level", fontsize=12)
        plt.plot(loss_s)
        plt.subplot(143)
        plt.title("industry-level", fontsize=12)
        plt.plot(loss_i)
        plt.subplot(144)
        plt.title("stock-level", fontsize=12)
        plt.plot(loss)
        plt.legend(["loss decay"], fontsize=12, loc="upper right")
        plt.xlabel("iteration", fontsize=12)
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Losses decay plot has been saved in this directory as {}.'.format(fig_name))
    return phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi

def softplus(x: np.array):
    """
    It is a function from real to positive numbers

    Parameters
    ----------
    x: np.array
        Real value.
    """
    return np.log(1 + np.exp(x))

def order_selection(logp: np.array, info: dict, orders: np.array = np.arange(1, 14), horizon: int = 5):
    """
    It is a function from real to positive numbers

    Parameters
    ----------
    logp: np.array
        Log-prices at stock-level.
    info: dict
        Data information.
    orders: np.array
        Array of candidate orders.
    horizon: int
        Number of days to evaluate prediction.
    """
    print("\nModel selection in progress. This can take a few minutes...")
    t = logp[:, :-horizon].shape[1]
    min_loss = np.inf
    count = 0
    for i, order in enumerate(orders):
        info['tt'] = (np.linspace(1 / t, 1, t) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
        info['order_scale'] = np.linspace(1 / (order + 1), 1, order + 1)[::-1].astype('float32')[None, :]

        # training the model
        phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi = training(logp[:, :-horizon], info)

        # construct loss
        tt_pred = ((1 + (np.arange(1, 1 + horizon) / t)) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
        logp_pred = np.dot(phi.numpy(), tt_pred)
        std_logp_pred = softplus(psi.numpy())
        scores = (logp_pred - logp[:, -horizon:]) / std_logp_pred
        loss = np.abs(np.mean(scores ** 2) - 1)

        print("Loss value for backtested polynomial model of order {}: {}.".format(order, loss))
        if i > 0 and loss > min_loss:
            count += 1
        else:
            min_loss = loss
            min_order = order
            count = 0
        if count == 3:
            break
    print("Model selection completed. Volatile will use a polynomial model of degree {}.".format(min_order))
    return min_order

if __name__ == '__main__':
    cli = ArgumentParser('Volatile: your day-to-day trading companion.',
                         formatter_class=ArgumentDefaultsHelpFormatter)
    cli.add_argument('-s', '--symbols', type=str, nargs='+', help='List of symbols.')
    cli.add_argument('--save-table', action='store_true',
                     help='Save prediction table in csv format.')
    cli.add_argument('--no-plots', action='store_true',
                     help='Plot estimates with their uncertainty over time.')
    cli.add_argument('--plot-losses', action='store_true',
                     help='Plot loss function decay over training iterations.')
    args = cli.parse_args()

    today = dt.date.today().strftime("%Y-%m-%d")

    print('\nDownloading all available closing prices in the last year...')
    if args.symbols is None:
        with open("symbols_list.txt", "r") as my_file:
            args.symbols = my_file.readlines()[0].split(" ")
    data = load_data(args.symbols)
    tickers = data["tickers"]
    num_stocks, t = data['logp'].shape

    # find unique names of sectors
    usectors = np.unique(data['sectors'])
    num_sectors = len(usectors)
    # provide sector IDs at stock-level
    sectors_id = [np.where(usectors == sector)[0][0] for sector in data['sectors']]
    # find unique names of industries and store indices
    uindustries, industries_idx = np.unique(data['industries'], return_index=True)
    num_industries = len(uindustries)
    # provide industry IDs at stock-level
    industries_id = [np.where(uindustries == industry)[0][0] for industry in data['industries']]
    # provide sector IDs at industry-level
    sector_industries_id = np.array(sectors_id)[industries_idx].tolist()

    # place relevant information in dictionary
    info = dict(num_sectors=num_sectors, num_industries=num_industries, sector_industries_id=sector_industries_id,
                industries_id=industries_id, sectors_id=sectors_id)

    # how many days to look ahead when comparing the current price against a prediction
    horizon = 5
    # order of the polynomial
    order = order_selection(data['logp'], info)

    print("\nTraining the model...")

    # times corresponding to trading dates in the data
    info['tt'] = (np.linspace(1 / t, 1, t) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
    # reweighing factors for parameters corresponding to different orders of the polynomial
    info['order_scale'] = np.linspace(1 / (order + 1), 1, order + 1)[::-1].astype('float32')[None, :]

    # training the model
    phi_m, psi_m, phi_s, psi_s, phi_i, psi_i, phi, psi = training(data['logp'], info, plot_losses=args.plot_losses)
    # calculate stock-level estimators of log-prices
    logp_est = np.dot(phi.numpy(), info['tt'])
    std_logp_est = softplus(psi.numpy())
    # calculate stock-level estimators of prices
    p_est = np.exp(logp_est + std_logp_est ** 2 / 2)
    std_p_est = np.sqrt(np.exp(2 * logp_est + std_logp_est ** 2) * (np.exp(std_logp_est ** 2) - 1))
    # calculate stock-level predictions of log-prices
    tt_pred = ((1 + (np.arange(1 + horizon) / t)) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')
    logp_pred = np.dot(phi.numpy(), tt_pred)
    std_logp_pred = softplus(psi.numpy())
    # calculate stock-level prediction of prices
    p_pred = np.exp(logp_pred + std_logp_pred ** 2 / 2)
    std_p_pred = np.sqrt(np.exp(2 * logp_pred + std_logp_pred ** 2) * (np.exp(std_logp_pred ** 2) - 1))
    # calculate industry-level estimators of log-prices
    logp_ind_est = np.dot(phi_i.numpy(), info['tt'])
    std_logp_ind_est = softplus(psi_i.numpy())
    # calculate industry-level estimators of prices
    p_ind_est = np.exp(logp_ind_est + std_logp_ind_est ** 2 / 2)
    std_p_ind_est = np.sqrt(np.exp(2 * logp_ind_est + std_logp_ind_est ** 2) * (np.exp(std_logp_ind_est ** 2) - 1))
    # calculate sector-level estimators of log-prices
    logp_sec_est = np.dot(phi_s.numpy(), info['tt'])
    std_logp_sec_est = softplus(psi_s.numpy())
    # calculate sector-level estimators of prices
    p_sec_est = np.exp(logp_sec_est + std_logp_sec_est ** 2 / 2)
    std_p_sec_est = np.sqrt(np.exp(2 * logp_sec_est + std_logp_sec_est ** 2) * (np.exp(std_logp_sec_est ** 2) - 1))
    # calculate market-level estimators of log-prices
    logp_mkt_est = np.dot(phi_m.numpy(), info['tt'])
    std_logp_mkt_est = softplus(psi_m.numpy())
    # calculate market-level estimators of prices
    p_mkt_est = np.exp(logp_mkt_est + std_logp_mkt_est ** 2 / 2)
    std_p_mkt_est = np.sqrt(np.exp(2 * logp_mkt_est + std_logp_mkt_est ** 2) * (np.exp(std_logp_mkt_est ** 2) - 1))

    print("Training completed.")

    # calculate score
    scores = ((logp_pred[:, horizon] - data["logp"][:, -1]) / std_logp_pred.squeeze())
    # rank according to score
    rank = np.argsort(scores)[::-1]
    ranked_tickers = np.array(tickers)[rank]
    ranked_scores = scores[rank]
    ranked_p = np.exp(data["logp"])[rank]
    ranked_p_est = p_est[rank]
    ranked_std_p_est = std_p_est[rank]
    ranked_p_pred = p_pred[rank]
    ranked_std_p_pred = std_p_pred[rank]
    
    # stock thresholds
    st = {"HIGHLY BELOW TREND": 3, "BELOW TREND": 2, "ALONG TREND": 0, "ABOVE TREND": -2, "HIGHLY ABOVE TREND": -3}
    # stock information
    si = {"HIGHLY BELOW TREND": np.where(ranked_scores > st["HIGHLY BELOW TREND"])[0],
              "BELOW TREND": np.where((ranked_scores <= st["HIGHLY BELOW TREND"]) & (ranked_scores > st["BELOW TREND"]))[0],
              "ALONG TREND": np.where((ranked_scores <= st["BELOW TREND"]) & (ranked_scores > st["ABOVE TREND"]))[0],
              "ABOVE TREND": np.where((ranked_scores <= st["ABOVE TREND"]) & (ranked_scores > st["HIGHLY ABOVE TREND"]))[0],
              "HIGHLY ABOVE TREND": np.where(ranked_scores <= st["HIGHLY ABOVE TREND"])[0]}
    si = {k: v[0] for k, v in si.items() if len(v) > 0}
    # rate all stocks
    ranked_rating = np.array(list(si.keys())).repeat(list(np.diff(list(si.values()))) + [num_stocks - list(si.values())[-1]]).tolist()

    if not args.no_plots:
        ## information for plotting
        # find unique names of sectors
        usectors = np.unique(data['sectors'])
        num_sectors = len(usectors)
        # determine which sectors were not available to avoid plotting
        NA_sectors = np.where(np.array([sec[:2] for sec in usectors]) == "NA")[0]
        num_NA_sectors = len(NA_sectors)
        # provide sector IDs at stock-level
        sectors_id = [np.where(usectors == sector)[0][0] for sector in data['sectors']]
        # find unique names of industries and store indices
        uindustries = np.unique(data['industries'])
        num_industries = len(uindustries)
        # determine which industries were not available to avoid plotting
        NA_industries = np.where(np.array([ind[:2] for ind in uindustries]) == "NA")[0]
        num_NA_industries = len(NA_industries)
        # provide industry IDs at stock-level
        industries_id = [np.where(uindustries == industry)[0][0] for industry in data['industries']]
        # ranked volume at stock level
        ranked_volume = data["volume"][rank]
        # number of volume dates
        vt = len(data["volume_dates"])

        print('\nPlotting market estimation...')
        fig = plt.figure(figsize=(10,3))
        left_mkt_est = np.maximum(0, p_mkt_est - 2 * std_p_mkt_est)
        right_mkt_est = p_mkt_est + 2 * std_p_mkt_est

        plt.title("Market", fontsize=15)
        l1 = plt.plot(data["stock_dates"], np.exp(data['logp'].mean(0)), label="avg. price", color="C0")
        l2 = plt.plot(data["stock_dates"], p_mkt_est[0], label="trend", color="C1")
        l3 = plt.fill_between(data["stock_dates"], left_mkt_est[0], right_mkt_est[0], alpha=0.2, label="+/- 2 st. dev.", color="C0")
        plt.ylabel("avg. price", fontsize=12)
        plt.twinx()
        l4 = plt.bar(data["volume_dates"], ranked_volume.mean(0), width=1, color='g', alpha=0.2, label='avg. volume')
        plt.ylabel("avg. volume", fontsize=12)
        ll = l1 + l2 + [l3] + [l4]
        labels = [l.get_label() for l in ll]
        plt.legend(ll, labels, loc="upper left")
        fig_name = 'market_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Market estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        num_columns = 3
        print('\nPlotting sector estimation...')
        left_sec_est = np.maximum(0, p_sec_est - 2 * std_p_sec_est)
        right_sec_est = p_sec_est + 2 * std_p_sec_est
        fig = plt.figure(figsize=(20, max(num_sectors - num_NA_sectors, 5)))
        j = 0
        for i in range(num_sectors):
            if i not in NA_sectors:
                j += 1
                plt.subplot(int(np.ceil((num_sectors - num_NA_sectors) / num_columns)), num_columns, j)
                plt.title(usectors[i], fontsize=15)
                idx_sectors = np.where(np.array(sectors_id) == i)[0]
                l1 = plt.plot(data["stock_dates"], np.exp(data['logp'][idx_sectors].reshape(-1, t).mean(0)), label="avg. price", color="C0")
                l2 = plt.plot(data["stock_dates"], p_sec_est[i], label="trend", color="C1")
                l3 = plt.fill_between(data["stock_dates"], left_sec_est[i], right_sec_est[i], alpha=0.2, label="+/- 2 st. dev.", color="C0")
                plt.ylabel("avg. price", fontsize=12)
                plt.xticks(rotation=45)
                plt.twinx()
                l4 = plt.bar(data["volume_dates"], data['volume'][np.where(np.array(sectors_id) == i)[0]].reshape(-1, vt).mean(0),
                             width=1, color='g', alpha=0.2, label='avg. volume')
                plt.ylabel("avg. volume", fontsize=12)
                ll = l1 + l2 + [l3] + [l4]
                labels = [l.get_label() for l in ll]
                plt.legend(ll, labels, loc="upper left")

        plt.tight_layout()
        fig_name = 'sector_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Sector estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        print('\nPlotting industry estimation...')
        left_ind_est = np.maximum(0, p_ind_est - 2 * std_p_ind_est)
        right_ind_est = p_ind_est + 2 * std_p_ind_est
        fig = plt.figure(figsize=(20, max(num_industries - num_NA_industries, 5)))
        j = 0
        for i in range(num_industries):
            if i not in NA_industries:
                j += 1
                plt.subplot(int(np.ceil((num_industries - num_NA_industries) / num_columns)), num_columns, j)
                plt.title(uindustries[i], fontsize=15)
                idx_industries = np.where(np.array(industries_id) == i)[0]
                plt.title(uindustries[i], fontsize=15)
                l1 = plt.plot(data["stock_dates"], np.exp(data['logp'][idx_industries].reshape(-1, t).mean(0)), label="avg. price", color="C0")
                l2 = plt.plot(data["stock_dates"], p_ind_est[i], label="trend", color="C1")
                l3 = plt.fill_between(data["stock_dates"], left_ind_est[i], right_ind_est[i], alpha=0.2, label="+/- 2 st. dev.", color="C0")
                plt.ylabel("avg. price", fontsize=12)
                plt.xticks(rotation=45)
                plt.twinx()
                l4 = plt.bar(data["volume_dates"], data['volume'][np.where(np.array(industries_id) == i)[0]].reshape(-1, vt).mean(0),
                             width=1, color='g', alpha=0.2, label='avg. volume')
                plt.ylabel("avg. volume", fontsize=12)
                ll = l1 + l2 + [l3] + [l4]
                labels = [l.get_label() for l in ll]
                plt.legend(ll, labels, loc="upper left")
        plt.tight_layout()
        fig_name = 'industry_estimation.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('Industry estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))

        # determine which stocks are along trend to avoid plotting them
        along_trend = np.where(np.array(ranked_rating) == "ALONG TREND")[0]
        num_out_trend = num_stocks - len(along_trend)

        if num_out_trend > 0:
            print('\nPlotting stock estimation...')
            ranked_left_est = np.maximum(0, ranked_p_est - 2 * ranked_std_p_est)
            ranked_right_est = ranked_p_est + 2 * ranked_std_p_est

            j = 0
            fig = plt.figure(figsize=(20, max(num_out_trend, 5)))
            for i in range(num_stocks):
                if i not in along_trend:
                    j += 1
                    plt.subplot(int(np.ceil(num_out_trend / num_columns)), num_columns, j)
                    plt.title(ranked_tickers[i], fontsize=15)
                    l1 = plt.plot(data["stock_dates"], ranked_p[i], label="price")
                    l2 = plt.plot(data["stock_dates"], ranked_p_est[i], label="trend")
                    l3 = plt.fill_between(data["stock_dates"], ranked_left_est[i], ranked_right_est[i], alpha=0.2,
                                          label="+/- 2 st. dev.")
                    plt.yticks(fontsize=12)
                    plt.xticks(rotation=45)
                    plt.ylabel("price", fontsize=12)
                    plt.twinx()
                    l4 = plt.bar(data["volume_dates"], ranked_volume[i], width=1, color='g', alpha=0.2, label='volume')
                    plt.ylabel("volume", fontsize=12)
                    ll = l1 + l2 + [l3] + [l4]
                    labels = [l.get_label() for l in ll]
                    plt.legend(ll, labels, loc="upper left")
            plt.tight_layout()
            fig_name = 'stock_estimation.png'
            fig.savefig(fig_name, dpi=fig.dpi)
            print('Stock estimation plot has been saved to {}/{}.'.format(os.getcwd(), fig_name))
        elif os.path.exists('stock_estimation.png'):
            os.remove('stock_estimation.png')

    print("\nPREDICTION TABLE")
    ranked_sectors = [name if name[:2] != "NA" else "Not Available" for name in np.array(data["sectors"])[rank]]
    ranked_industries = [name if name[:2] != "NA" else "Not Available" for name in np.array(data["industries"])[rank]]
    num_dashes = 126
    print(num_dashes * "-")
    print("{:<11} {:<26} {:<42} {:<25} {:<15}".format("SYMBOL", "SECTOR", "INDUSTRY", "LAST AVAILABLE PRICE", "RATING"))
    print(num_dashes * "-")
    for i in range(num_stocks):
        print("{:<11} {:<26} {:<42} {:<25} {:<15}".format(ranked_tickers[i], ranked_sectors[i],
                                                                        ranked_industries[i], ranked_p[i, -1],
                                                                        ranked_rating[i]))
        print(num_dashes * "-")
        if i + 1 in si.values():
            print(num_dashes * "-")

    if args.save_table:
        tab_name = 'prediction_table.csv'
        table = zip(["SYMBOL"] + ranked_tickers.tolist(),
                    ['SECTOR'] + ranked_sectors,
                    ['INDUSTRY'] + ranked_industries,
                    ["LAST AVAILABLE PRICE"] + ranked_p[:, -1].tolist(),
                    ["RATING"] + ranked_rating)
        with open(tab_name, 'w') as file:
            wr = csv.writer(file)
            for row in table:
                wr.writerow(row)
        print('\nThe prediction table printed above has been saved to {}/{}.'.format(os.getcwd(), tab_name))
