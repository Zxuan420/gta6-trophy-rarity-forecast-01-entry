#!/usr/bin/env python3
import os
import sys
import json
import math
from datetime import datetime, timezone, timedelta

LAUNCH = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone.utc)
Z = [-1.645, -0.674, 0, 0.674, 1.645]
SIGMA_BASE = 0.55
DAMP = 0.85
SHRINK_K = 3.0

def parse_iso(s):
    if not s:
        return LAUNCH
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    return datetime.fromisoformat(s)

def clamp_pct(x):
    return min(99.99, max(0.01, x))

def logit(pct):
    cp = clamp_pct(pct)
    return math.log(cp / (100.0 - cp))

def inv_logit(l):
    try:
        if l >= 35.0:
            return 100.0
        if l <= -35.0:
            return 0.0
        return 100.0 / (1.0 + math.exp(-l))
    except OverflowError:
        return 100.0 if l > 0 else 0.0

def round4(x):
    v = math.floor(x * 10000 + 0.5) / 10000.0
    if float(v).is_integer():
        return int(v)
    return v

def median(lst):
    if not lst:
        return 0.0
    s = sorted(lst)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    else:
        return 0.5 * (s[n // 2 - 1] + s[n // 2])

def ols(points):
    if len(points) < 2:
        return None
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    var_x = sum_xx - (sum_x ** 2) / n
    if abs(var_x) < 1e-12:
        return None
    slope = (sum_xy - (sum_x * sum_y) / n) / var_x
    intercept = (sum_y - slope * sum_x) / n
    residuals = [p[1] - (slope * p[0] + intercept) for p in points]
    mean_resid = sum(residuals) / n
    var_resid = sum((r - mean_resid) ** 2 for r in residuals) / n
    resid_pstdev = math.sqrt(max(0.0, var_resid))
    return slope, intercept, resid_pstdev

def x_of(dt):
    hours = (dt - LAUNCH).total_seconds() / 3600.0
    return math.log(1.0 + max(0.0, hours))

def run_forecast(case):
    history = case.get('history', [])
    target_str = case.get('target')
    at_str = case.get('at')
    horizon_min = case.get('horizon_minutes')
    trophies = case.get('trophies', [])
    
    at_dt = parse_iso(at_str) if at_str else LAUNCH
    if target_str:
        target_dt = parse_iso(target_str)
    elif horizon_min is not None:
        target_dt = at_dt + timedelta(minutes=horizon_min)
    else:
        target_dt = at_dt + timedelta(days=1)
        
    x_target = x_of(target_dt)
    
    if horizon_min is not None:
        horizon_days = horizon_min / 1440.0
    else:
        delta_days = (target_dt - at_dt).total_seconds() / 86400.0
        horizon_days = max(0.01, delta_days)
        
    distinct_states = []
    last_key = None
    for read in history:
        players = read.get('players_tracked')
        rates = read.get('rates')
        rates_tuple = tuple(rates) if rates is not None else ()
        key = (players, rates_tuple)
        if key != last_key:
            at_dt_read = parse_iso(read['at']) if 'at' in read else at_dt
            x_val = x_of(at_dt_read)
            distinct_states.append({
                'at': at_dt_read,
                'x': x_val,
                'players': players if players is not None else 0,
                'rates': rates if rates is not None else []
            })
            last_key = key
            
    if not distinct_states:
        raise ValueError("No history states")
        
    players_points = []
    for st in distinct_states:
        p = st['players']
        if p > 0:
            players_points.append((st['x'], math.log(p)))
            
    slope_p = 0.0
    last_x = distinct_states[-1]['x']
    actual_players_last = distinct_states[-1]['players'] if distinct_states[-1]['players'] > 0 else 1.0
    
    ols_p = ols(players_points)
    if ols_p is not None:
        slope_p, _, _ = ols_p
        
    logP_last = math.log(max(1.0, actual_players_last))
    logP_target = logP_last + slope_p * DAMP * (x_target - last_x)
    logP_target = max(logP_last - 0.2, min(logP_last + 3.0, logP_target))
    P_target = math.exp(logP_target)
    if not players_points:
        P_target = 1.0
        
    valid_bs = []
    trophy_results = []
    
    for i, trophy in enumerate(trophies):
        t_points = []
        e_last = 0.0
        rate_last = 0.0
        x_last_i = last_x
        
        for st in distinct_states:
            rates = st['rates']
            if i < len(rates) and rates[i] is not None:
                r = rates[i]
                p = st['players']
                e = (r / 100.0) * p
                t_points.append((st['x'], math.log(e + 0.5)))
                e_last = e
                rate_last = r
                x_last_i = st['x']
                
        res_ols = ols(t_points)
        b_i = None
        resid_sd = 0.0
        if res_ols is not None:
            b_i, _, resid_sd = res_ols
            if len(t_points) < 3:
                resid_sd = 0.0
        else:
            resid_sd = 0.0
            
        if b_i is not None:
            valid_bs.append(b_i)
            
        trophy_results.append({
            'id': trophy['id'],
            't_points': t_points,
            'b_i': b_i,
            'resid_sd': resid_sd,
            'e_last': e_last,
            'rate_last': rate_last,
            'x_last_i': x_last_i,
            'n': len(t_points)
        })
        
    b_med = median(valid_bs) if valid_bs else slope_p
    
    forecast_trophies = []
    for tr in trophy_results:
        n = tr['n']
        b_i = tr['b_i']
        if b_i is not None:
            b_shrunk = (n / (n + SHRINK_K)) * b_i + (SHRINK_K / (n + SHRINK_K)) * b_med
        else:
            b_shrunk = b_med
        b_shrunk = max(-0.5, min(3.0, b_shrunk))
        
        # As a trophy's earn rate nears 100%, its earners growth must converge
        # to the player-base growth, so the excess slope is scaled by the remaining headroom (1 - rate/100).
        sat = 1.0 - (tr['rate_last'] / 100.0)
        sat = max(0.0, min(1.0, sat))
        b_eff = slope_p + (b_shrunk - slope_p) * sat

        log_e_target = math.log(tr['e_last'] + 0.5) + b_eff * DAMP * (x_target - tr['x_last_i'])
        e_target = max(0.0, math.exp(log_e_target) - 0.5)
        rate_target = 100.0 * e_target / P_target if P_target > 0 else 0.0
        median_rate = min(99.3, max(0.02, rate_target))
        
        base = SIGMA_BASE * math.sqrt(max(0.0, horizon_days))
        count_noise = min(0.6, 1.0 / math.sqrt(max(tr['e_last'], 1.0)))
        sigma = math.sqrt(base ** 2 + (0.8 * tr['resid_sd']) ** 2 + count_noise ** 2)
        sigma = max(0.5, min(2.2, sigma))
        
        if tr['rate_last'] >= 99.9:
            median_rate = 99.3
            sigma = max(sigma, 1.6)
            
        if tr['n'] == 0:
            median_rate = 0.02
            sigma = 2.2
            
        mlogit = logit(median_rate)
        q = []
        prev = 0.0
        for z in Z:
            val = round4(inv_logit(mlogit + z * sigma))
            val = max(prev, val)
            val = max(0.0, min(100.0, val))
            q.append(round4(val))
            prev = val
            
        forecast_trophies.append({
            'id': tr['id'],
            'q': q
        })
        
    return {'trophies': forecast_trophies}

def persistence_forecast(case):
    trophies = case.get('trophies', [])
    history = case.get('history', [])
    horizon_min = case.get('horizon_minutes', 1440)
    horizon_days = horizon_min / 1440.0
    spread = 0.5 * math.sqrt(max(0.0, horizon_days))
    
    last_rates = {}
    if history:
        last_read = history[-1]
        rates = last_read.get('rates', [])
        for i, tr in enumerate(trophies):
            r = rates[i] if i < len(rates) and rates[i] is not None else 0.0
            last_rates[tr['id']] = r
            
    res_trophies = []
    for tr in trophies:
        r = last_rates.get(tr['id'], 0.0)
        mlogit = logit(r)
        q = []
        prev = 0.0
        for z in Z:
            val = round4(inv_logit(mlogit + z * spread))
            val = max(prev, val)
            val = max(0.0, min(100.0, val))
            q.append(round4(val))
            prev = val
        res_trophies.append({
            'id': tr['id'],
            'q': q
        })
    return {'trophies': res_trophies}

def main():
    input_dir = os.environ.get('MAKEFUN_INPUT_DIR', '/input')
    output_dir = os.environ.get('MAKEFUN_OUTPUT_DIR', '/output')
    
    os.makedirs(output_dir, exist_ok=True)
    
    if not os.path.exists(input_dir):
        return
        
    in_files = sorted([f for f in os.listdir(input_dir) if f.endswith('.in')])
    for fname in in_files:
        in_path = os.path.join(input_dir, fname)
        stem = os.path.splitext(fname)[0]
        out_path = os.path.join(output_dir, f"{stem}.out")
        
        try:
            with open(in_path, 'r', encoding='utf-8') as f:
                case = json.load(f)
        except Exception:
            case = {}
            
        try:
            forecast = run_forecast(case)
        except Exception:
            forecast = persistence_forecast(case)
            
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(forecast, separators=(',', ':')) + '\n')

if __name__ == '__main__':
    main()
