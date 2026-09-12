from datetime import timedelta
from importlib.metadata import version
from math import cos, pi, sin, sqrt

from .schema import InputError, day


ADVANCED_ENGINES = {'lightgbm', 'catboost'}
FEATURES = ['lag_1', 'lag_7', 'lag_14', 'mean_7', 'mean_14', 'weekday_sin', 'weekday_cos', 'month_sin', 'month_cos', 'elapsed_days']
MINIMUM_DAYS = 56
HOLDOUT_DAYS = 14
CONTRACT = 'daily-lags-calendar-temporal-v1'


def contiguous_history(observations: dict[str, float], start_date: str) -> tuple[list[str], list[float]]:
    cursor = day(start_date) - timedelta(days=1)
    rows = []
    while cursor.isoformat() in observations and len(rows) < 730:
        rows.append((cursor.isoformat(), observations[cursor.isoformat()]))
        cursor -= timedelta(days=1)
    rows.reverse()
    return [row[0] for row in rows], [row[1] for row in rows]


def validate_advanced(observations: dict[str, float], start_date: str) -> None:
    _, values = contiguous_history(observations, start_date)
    if len(values) < MINIMUM_DAYS:
        raise InputError(f'Advanced forecasts require at least {MINIMUM_DAYS} consecutive observed daily quantities ending the day before the forecast. This scope supplies {len(values)}. Record confirmed zero-demand days explicitly; missing days are not zero.')


def features(values: list[float], forecast_date: str, origin: str) -> list[float]:
    d = day(forecast_date)
    return [values[-1], values[-7], values[-14], sum(values[-7:]) / 7, sum(values[-14:]) / 14,
            sin(2 * pi * d.weekday() / 7), cos(2 * pi * d.weekday() / 7),
            sin(2 * pi * (d.month - 1) / 12), cos(2 * pi * (d.month - 1) / 12), (d - day(origin)).days]


def fit_model(engine: str, calendar: list[str], values: list[float]):
    import numpy as np
    x = np.asarray([features(values[:i], calendar[i], calendar[0]) for i in range(14, len(values))], dtype=float)
    y = np.asarray(values[14:], dtype=float)
    if engine == 'lightgbm':
        from lightgbm import LGBMRegressor
        model = LGBMRegressor(n_estimators=120, learning_rate=0.05, num_leaves=15, max_depth=4,
                              min_child_samples=5, random_state=42, n_jobs=1, verbosity=-1,
                              deterministic=True, force_col_wise=True)
    elif engine == 'catboost':
        from catboost import CatBoostRegressor
        model = CatBoostRegressor(iterations=120, depth=4, learning_rate=0.05, loss_function='RMSE',
                                  random_seed=42, thread_count=1, verbose=False, has_time=True,
                                  allow_writing_files=False, allow_const_label=True, boosting_type='Ordered')
    else:
        raise InputError('Choose a supported advanced forecast engine.')
    model.fit(x, y)
    return model


def model_path(model, history: list[float], start: str, steps: int, origin: str) -> list[float]:
    import numpy as np
    values = history.copy()
    output = []
    for i in range(steps):
        prediction = float(model.predict(np.asarray([features(values, (day(start) + timedelta(days=i)).isoformat(), origin)], dtype=float))[0])
        prediction = max(0.0, prediction)
        output.append(round(prediction, 6))
        values.append(prediction)
    return output


def errors(actual: list[float], predicted: list[float]) -> dict:
    differences = [prediction - observation for observation, prediction in zip(actual, predicted, strict=True)]
    absolute = sum(abs(value) for value in differences)
    total = sum(abs(value) for value in actual)
    return {'mae': round(absolute / len(actual), 6), 'rmse': round(sqrt(sum(value * value for value in differences) / len(actual)), 6),
            'bias': round(sum(differences) / len(actual), 6), 'wape_percent': round(absolute / total * 100, 6) if total else None}


def build_forecast(config: dict, observations: dict[str, float], mode: str) -> dict:
    engine, start, horizon = config['engine'], config['start_date'], config['horizon_days']
    calendar, history = contiguous_history(observations, start)
    lag = int(config.get('assumptions', {}).get('season_length_days') or 7)
    advanced = engine in ADVANCED_ENGINES
    if advanced:
        validate_advanced(observations, start)
        model = fit_model(engine, calendar, history)
        values = model_path(model, history, start, horizon, calendar[0])
    elif engine == 'naive':
        values = [observations[max(observations)]] * horizon
    else:
        cycle = [(day(start) - timedelta(days=lag - i)).isoformat() for i in range(lag)]
        if any(d not in observations for d in cycle):
            raise InputError(f'Seasonal naive requires a complete {lag}-day prior season.')
        values = [observations[cycle[i % lag]] for i in range(horizon)]
    diagnostics = {'contract_version': CONTRACT, 'evaluation_source': 'Synthetic demonstration records' if mode == 'demo' else 'Submitted business observations',
                   'engine': engine, 'library_version': version(engine) if advanced else 'builtin-v1',
                   'feature_names': FEATURES if advanced else ['last_observation' if engine == 'naive' else f'lag_{lag}'],
                   'history_start': min(observations), 'history_end': max(observations), 'observed_days': len(observations),
                   'consecutive_training_days': len(history), 'training_rows': max(0, len(history) - 14) if advanced else len(history),
                   'training_cutoff': calendar[-1] if calendar else max(observations),
                   'temporal_method': 'Final model fits all eligible history before the forecast start. Backtest holds out the last 14 consecutive observed dates; fitting never sees their targets. The entire held-out path is predicted recursively without actual-target feedback.',
                   'formulas': {'mae': 'mean(abs(forecast - actual)), in product units', 'rmse': 'sqrt(mean((forecast - actual)^2)), in product units',
                                'bias': 'mean(forecast - actual); positive means over-forecast, in product units', 'wape_percent': '100 * sum(abs(forecast - actual)) / sum(abs(actual)); unavailable when the denominator is zero'},
                   'backtest': None,
                   'limitations': ['Calendar and lagged demand are the configured drivers. Price, promotion and future availability are not inferred.', 'No calibrated probability interval is produced.', 'Recorded sales may be censored by stockouts.', 'Small local models are not optimized or hierarchically reconciled. Advanced does not imply more accurate.']}
    minimum_backtest = 56 if advanced else HOLDOUT_DAYS + (lag if engine == 'seasonal-naive' else 1)
    if len(history) >= minimum_backtest:
        train_dates, train = calendar[:-HOLDOUT_DAYS], history[:-HOLDOUT_DAYS]
        actual = history[-HOLDOUT_DAYS:]
        if advanced:
            evaluation_model = fit_model(engine, train_dates, train)
            predicted = model_path(evaluation_model, train, calendar[-HOLDOUT_DAYS], HOLDOUT_DAYS, calendar[0])
        else:
            predicted = [train[-1]] * HOLDOUT_DAYS if engine == 'naive' else [train[-lag:][i % lag] for i in range(HOLDOUT_DAYS)]
        naive = [train[-1]] * HOLDOUT_DAYS
        seasonal = [train[-7:][i % 7] for i in range(HOLDOUT_DAYS)] if len(train) >= 7 else None
        diagnostics['backtest'] = {'start_date': calendar[-HOLDOUT_DAYS], 'end_date': calendar[-1], 'training_end': train_dates[-1], 'observations': HOLDOUT_DAYS,
                                   'metrics': errors(actual, predicted), 'naive_metrics': errors(actual, naive), 'seasonal_naive_metrics': errors(actual, seasonal) if seasonal else None,
                                   'series': [{'date': d, 'actual': a, 'predicted': p, 'naive': n, **({'seasonal_naive': seasonal[i]} if seasonal else {})} for i, (d, a, p, n) in enumerate(zip(calendar[-HOLDOUT_DAYS:], actual, predicted, naive, strict=True))]}
    else:
        diagnostics['limitations'].append(f'No backtest is shown: this engine needs {minimum_backtest} consecutive daily observations for the documented held-out evaluation.')
    return {'values': values, 'diagnostics': diagnostics, 'history': [{'date': d, 'actual': observations[d]} for d in sorted(observations)[-365:]]}
