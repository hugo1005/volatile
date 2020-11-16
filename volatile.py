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


def load_data(tickers, start, end):
    tickers = list(set(tickers))
    stocks = yf.Tickers(tickers)

    df = stocks.download(start=start, end=end)['Close'].dropna(1)
    missing_tickers = [tick for tick in tickers if tick not in df.columns]
    if len(missing_tickers) > 0:
        print('\nRemoving {} from list of symbols because yahoo-finance could not provide full information.'.format(
            missing_tickers))
    tickers = list(df.columns)
    stocks = yf.Tickers(tickers)

    logp = np.log(df.to_numpy().T)
    # listed = np.where(np.sum(np.isnan(logp), 1) == 0)[0].tolist()
    # logp = logp[listed]
    # print(len(tickers), logp.shape)
    filename = "stock_info.csv"
    print('\nAccessing stock information. For all the symbols that you download for the first time, this can take a '
          'while (blame yahoo-finance). Otherwise, stock information is cached into ' + filename + ' and it will be '
          'fast.\n')

    if not os.path.exists(filename):
        with open(filename, 'w') as file:
            wr = csv.writer(file)
            for row in zip(["SYMBOL"], ["SECTOR"]):
                wr.writerow(row)
    stock_info = pd.read_csv(filename)

    sector_name = []
    missing_sector_info = {}
    for i in range(len(tickers)):
        idx = np.where(tickers[i] in stock_info["SYMBOL"].values)[0]
        if len(idx) > 0:
            sector_name.append(stock_info["SECTOR"][idx[0]])
        else:
            try:
                sector_name.append(stocks.tickers[i].info["sector"])
                missing_sector_info[tickers[i]] = sector_name[-1]
            except:
                sector_name.append("NA" + str(i))
    sectors = np.unique(sector_name)
    sector_id = [np.where(sectors == sector)[0][0] for sector in sector_name]

    sector_info = zip(list(missing_sector_info.keys()), list(missing_sector_info.values()))
    filename = "sector_info.csv"
    with open(filename, 'a+', newline='') as file:
        wr = csv.writer(file)
        for row in sector_info:
            wr.writerow(row)

    return dict(tickers=tickers, dates=df.index.date, sector_id=sector_id, logp=logp)


if __name__ == '__main__':
    cli = ArgumentParser('Volatile: your day-to-day companion   for financial stock trading.',
                         formatter_class=ArgumentDefaultsHelpFormatter)
    cli.add_argument('-s', '--symbols', type=str, nargs='+', help='List of symbols.')
    cli.add_argument('-pe', '--plot-estimation', type=bool, default=True,
                     help='Plot estimates and uncertainty between start and current date.')
    cli.add_argument('-sp', '--save-prediction', type=bool, default=True,
                     help='Save prediction table in csv format.')
    args = cli.parse_args()

    today = dt.date.today().strftime("%Y-%m-%d")
    tomorrow = (dt.date.today() + dt.timedelta(1)).strftime("%Y-%m-%d")
    one_year_ago = (dt.date.today() - dt.timedelta(365)).strftime("%Y-%m-%d")

    print('\nDownloading all available closing prices between ' + one_year_ago + ' and ' + today + '...')
    data = load_data(args.symbols, start=one_year_ago, end=tomorrow)
    tickers = data["tickers"]
    num_sectors = len(set(data["sector_id"]))
    num_stocks = data["logp"].shape[0]

    order = 12
    horizon = 5

    t = data["logp"].shape[1]
    tt = (np.linspace(1 / t, 1, t) ** np.arange(order + 1).reshape(-1, 1)).astype('float32')

    model = tfd.JointDistributionSequential([
        # phi_s
        tfd.Independent(tfd.Normal(loc=tf.zeros([num_sectors, 1]), scale=1), 2),
        # phi
        lambda phi_s: tfd.Independent(
            tfd.Normal(loc=tf.gather(phi_s, data["sector_id"], axis=0),
                       scale=np.linspace(1 / (order + 1), 1, order + 1)[::-1].astype('float32')[None, :]), 2),
        # psi_s
        tfd.Independent(tfd.Normal(loc=0, scale=tf.ones([num_sectors, 1])), 2),
        # psi
        lambda psi_s: tfd.Independent(tfd.Normal(loc=tf.gather(psi_s, data["sector_id"], axis=0), scale=1), 2),
        # y
        lambda psi, psi_s, phi: tfd.Independent(tfd.Normal(loc=tf.tensordot(phi, tt, axes=1),
                                                           scale=tf.math.softplus(psi + 1 - tt[1])), 2)])

    print("\nTraining the model...")
    tt_pred = np.arange(1 + horizon) / t

    attempt = 0
    while attempt < 2:
        phi_s, phi, psi_s, psi = (tf.Variable(model.sample()[:-1][i]) for i in range(4))
        log_posterior = lambda phi_s, phi, psi_s, psi: model.log_prob([phi_s, phi, psi_s, psi, data["logp"]])
        loss = tfp.math.minimize(lambda: -log_posterior(phi_s, phi, psi_s, psi),
                                 optimizer=tf.optimizers.Adam(learning_rate=0.01),
                                 num_steps=10000)

        logp_est = np.dot(phi.numpy(), tt)
        std_logp_est = np.log(1 + np.exp(psi.numpy() + 1 - tt[1]))
        p_est = np.exp(logp_est + std_logp_est ** 2 / 2)
        std_p_est = np.sqrt(np.exp(2 * logp_est + std_logp_est ** 2) * (np.exp(std_logp_est ** 2) - 1))


        logp_pred = np.dot(phi.numpy(), np.array([1 + tt_pred]) ** np.arange(order + 1)[:, None])
        std_logp_pred = np.log(1 + np.exp(psi.numpy() + tt_pred))
        p_pred = np.exp(logp_pred + std_logp_pred ** 2 / 2)
        std_p_pred = np.sqrt(np.exp(2 * logp_pred + std_logp_pred ** 2) * (np.exp(std_logp_pred ** 2) - 1))

        scores = ((logp_pred[:, horizon] - data["logp"][:, -1]) / std_logp_pred[:, horizon])

        if np.max(np.abs(scores) > 5):
            attempt += 1
        else:
            break

    rank = np.argsort(scores)[::-1]
    ranked_tickers = np.array(tickers)[rank]
    ranked_scores = scores[rank]
    ranked_p = np.exp(data["logp"])[rank]
    ranked_p_est = p_est[rank]
    ranked_std_p_est = std_p_est[rank]
    ranked_p_pred = p_pred[rank]
    ranked_std_p_pred = std_p_pred[rank]

    if args.plot_estimation:
        ranked_left_est = np.maximum(0, ranked_p_est - 2 * ranked_std_p_est)
        ranked_right_est = ranked_p_est + 2 * ranked_std_p_est

        num_columns = 3
        fig = plt.figure(figsize=(20, max(num_stocks, 3)))
        plt.suptitle("Price estimation between " + str(data["dates"][0])[:10] + " and " + str(data["dates"][-1])[:10]
                     + " via a " + str(order) + "-order polynomial regression", y=1.01, fontsize=20)
        for i in range(num_stocks):
            plt.subplot(num_stocks // 3 + 1, num_columns, i + 1)
            plt.title(ranked_tickers[i], fontsize=15)
            plt.plot(data["dates"], ranked_p[i], label="data")
            plt.plot(data["dates"], ranked_p_est[i], label="estimation")
            plt.fill_between(data["dates"], ranked_left_est[i], ranked_right_est[i], alpha=0.2, label="+/- 2 st. dev.")
            plt.yticks(fontsize=12)
            plt.xticks(rotation=45)
            plt.legend(loc="upper left")
        plt.tight_layout()
        fig_name = 'estimation_plots_' + today + '.png'
        fig.savefig(fig_name, dpi=fig.dpi)
        print('\nStock estimation plots have been saved in this directory as {}.'.format(fig_name))

    st = {"STRONG BUY": 3, "BUY": 2, "NEUTRAL": 0, "SELL": -2, "STRONG SELL": -3}
    si = {"STRONG BUY": np.where(ranked_scores > st["STRONG BUY"])[0],
              "BUY": np.where((ranked_scores <= st["STRONG BUY"]) & (ranked_scores > st["BUY"]))[0],
              "NEUTRAL": np.where((ranked_scores <= st["BUY"]) & (ranked_scores > st["SELL"]))[0],
              "SELL": np.where((ranked_scores <= st["SELL"]) & (ranked_scores > st["STRONG SELL"]))[0],
              "STRONG SELL": np.where(ranked_scores <= st["STRONG SELL"])[0]}
    si = {k: v[0] for k, v in si.items() if len(v) > 0}
    ranked_rating = np.array(list(si.keys())).repeat(list(np.diff(list(si.values()))) + [num_stocks - list(si.values())[-1]]).tolist()
    print("\nRANKING SYMBOLS FROM MOST TO LEAST POTENTIALLY PROFITABLE")
    print(56 * "--")
    print("{:<11} {:<25} {:<25} {:<37} {:<15}".format("SYMBOL", "PRICE ON " + str(data["dates"][-1]), "PREDICTED NEXT PRICE",
                                              "STANDARD DEVIATION OF PREDICTION", "RATING"))
    print(56 * "--")
    for i in range(num_stocks):
        print("{:<11} {:<25} {:<25} {:<37} {:<15}".format(ranked_tickers[i], ranked_p[i, -1],
                                                  ranked_p_pred[i, 1], ranked_std_p_pred[i, 1], ranked_rating[i]))
        print(56 * "--")

    if args.save_prediction:
        tab_name = 'prediction_data_' + today + '.csv'
        table = zip(["SYMBOLS"] + ranked_tickers.tolist(),
                    ["PRICE ON " + str(data["dates"][-1])] + ranked_p[:, -1].tolist(),
                    ["PREDICTED NEXT PRICE"] + ranked_p_pred[:, 1].tolist(),
                    ["STANDARD DEVIATION OF PREDICTION"] + ranked_std_p_pred[:, 1].tolist(),
                    ["RATING"] + ranked_rating)
        with open(tab_name, 'w') as file:
            wr = csv.writer(file)
            for row in table:
                wr.writerow(row)
        print('\nThe prediction data printed above has been saved in this directory as {}.'.format(tab_name))

