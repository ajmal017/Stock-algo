#Functions for the algos
import populate_database as db
import time
from models import Trade
import pandas as pd
import talib




    #wl_code is the watchlist code that identifies to which wl should be updated.
def get_watchlist_price(watchlist_df, wl_code, apis, server):
     
    watchlist_bars = apis.alpacaApi.get_barset(watchlist_df.ticker,'minute',limit = 1).df
    
    #The API that returns real time data is not perfect in my opinion. 
    #It returns the last minute candle that the given tickes has traded, not neccecarliy the latest
    #This has led me to do this wierd contraption where i fill the df's all NANs with the mean, and then I can just transpose it and take the first column
    #Since all the rows contain the same values after the fill.na with mean. 
    #You can set the start and end dates on the API call but I'm not sure if it supports minutes, since that is what I would be interested in.
    #This should not have been a problem but it it was

    watchlist_bars = watchlist_bars.fillna(watchlist_bars.mean())
    
    #We only want the "close" values, this is the first way I could come up with. Surely there is better.
    close_columns = [col for col in watchlist_bars.columns if "close" in col]
     
    close_values = watchlist_bars[close_columns].transpose().iloc[:,0]
    
    watchlist_df["current_price"] = list(close_values)
    watchlist_df["price_difference"] = watchlist_df["price"]- watchlist_df["current_price"]
    
    #Update the db prices 
    db.write_data_to_sql(pd.DataFrame(watchlist_df),wl_code+"_watchlist", server.serverSite)
    
    found_trades_long = []
    found_trades_short = []
    
    longs = watchlist_df[watchlist_df["side"].str.match("buy")]
    shorts = watchlist_df[watchlist_df["side"].str.match("sell")]
    
    for index, stock in longs.iterrows():
        if (stock["price_difference"] < 0 ):
            found_trades_long.append(stock["ticker"])
            

    for index, stock in shorts.iterrows():
        if (stock["price_difference"] > 0):
            found_trades_short.append(stock["ticker"])

    return found_trades_long, found_trades_short


#Define number of shares here
def fire_orders(trades, side, now, strategy,apis ,maxPosValue, maxPosSize, algo = "charlie"):
    
    current_bp = int(float(apis.alpacaApi.get_account().buying_power))

    succesful_trades = []
    for trade in trades:
        try:
            
            postValue = maxPosValue
            posSize = maxPosSize
    
            #Setting max pos size. Either trade value is 8000 or 500 shares. Which ever is bigger.
            current_price = apis.alpacaApi.get_barset(trade,"1Min",limit = 1).df.iloc[0,3]
            if (current_price * posSize >postValue):
                posSize = int(maxPosValue/current_price)
                
            #Check buying power, if not enough brake loop.    
            if(current_bp < current_price + posSize):
                print("No buying power")
                break
            
            live_trade = Trade(trade, posSize, side, now, strategy)
            
            
            live_trade.submitOrder(apis, algo = algo)
            succesful_trades.append(live_trade)
        except:
            print("Trade failed for ",trade)
    return succesful_trades



def current_active_trade_prices(current_trades, apis):
    
    #Get the latest 15min candle. Future trade decisions is made on the OHLC on it.
    for trade in current_trades:
        current_candle = apis.alpacaApi.get_barset(trade.ticker,"15Min",limit = 1).df
        trade.setLastCandle(current_candle)
        trade.setPosition(apis)
        
        

def check_stoploss(current_trades,ema_time_period, server,apis, algo = "charlie"):
    #Note to self. Search all data at once, not every stock for themself.
    #Find stop prices for the trades.
    for trade in current_trades:
        if (trade.stopPrice == 0) :
            data = db.read_from_database("Select date, ticker, uHigh, uLow, uClose from dailydata where ticker ='"+ trade.ticker+ "' ORDER BY date DESC limit "+str(ema_time_period+10)+";",server.serverSite)
            
            #Talib need the oldest data to be first     
            data = data.iloc[::-1]
        
            #Setting the stop price to the 20EMA
            data["stop_price"] = talib.EMA(data.uClose, timeperiod = ema_time_period)
            trade.setStopPrice(data.stop_price[0])
            print("Stop price for ", trade.ticker," is set to ", trade.stopPrice)
        else:
            #Get the close price of the last 5 minute candle and comapre it against the stop price
            #If the 5min candle has closed above the stop price, it will flatten the trade.
            #current_trade_price = apis.alpacaApi.get_barset(trade.ticker,"5Min",limit = 1).df.iloc[0,3]
            current_trade_price = trade.last15MinCandle.iloc[0,3]
            if (current_trade_price > trade.stopPrice  and trade.orderSide == "sell"):
                trade.flattenOrder(action = "Stoploss", apis = apis, server = server, algo = algo)
                current_trades.remove(trade)
            if (current_trade_price < trade.stopPrice and trade.orderSide == "buy"):
                trade.flattenOrder(action = "Stoploss", apis = apis, server = server, algo= algo)
                current_trades.remove(trade)
                
                
    return current_trades

def check_target(current_trades,apis, server, algo = "charlie"):
    #Set target price, and check if target price, current target is 2:1
    for trade in current_trades:
        if (trade.targetPrice  == 0):
            
            #update the current position info, sleep for a while so that the orders have time get filled.
            time.sleep(5)
            trade.setPosition(apis)
            
            if(trade.orderSide == "buy"):
                trade.targetPrice = trade.entryPrice + ((trade.entryPrice - trade.stopPrice)*2) 
            else:
                trade.targetPrice = trade.entryPrice - ((trade.stopPrice - trade.entryPrice)*2)
            
            print("Target price for ", trade.ticker," is set to ", trade.targetPrice)
        else:
            #Close the trade if the 1min candle high has hit the target
            current_trade_price = trade.last15MinCandle.iloc[0,1]
            current_trade_price_low = trade.last15MinCandle.iloc[0,2]
            if (current_trade_price_low < trade.targetPrice and trade.orderSide == "sell"):
                trade.flattenOrder(action = "Target",apis = apis,server = server, algo = algo)
                current_trades.remove(trade)
            if (current_trade_price > trade.targetPrice and trade.orderSide == "buy"):
                trade.flattenOrder(action = "Target", apis = apis, server = server, algo = algo)
                current_trades.remove(trade)
    
    return current_trades



def active_trades_to_db(active_trades, serverSite, table_name ="active_trades"):
    
    active_trade_lists = []
    for trade in active_trades:
        tradeinfo = [trade.ticker, trade.posSize, trade.entryPrice, trade.stopPrice,trade.targetPrice,trade.strategy,trade.unrealPL]
        active_trade_lists.append(tradeinfo)
    
    colnames = ["ticker","PosSize","EntryPrice","StopPrice","TargetPrice","Strategy","UnrealPl"]
    active_trade_df = pd.DataFrame(active_trade_lists, columns = colnames)
    db.write_data_to_sql(active_trade_df,table_name, serverSite)

    
            
            

    
