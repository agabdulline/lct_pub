"""Causal peer context. Registry adjacency is a proxy, never a fault label."""
import re
import numpy as np
import pandas as pd

META_CATS = ['object_key', 'loop_key']
META_NUM = ['picket_number', 'picket_offset', 'picket_unambiguous']
PEER_WINDOWS = {'smoke': (300, 900, 3600, 86400, 604800),
                'fault': (3600, 86400), 'unknown': (3600,), 'events': (3600,)}
STATE = {'smoke': 'Обнаружен дым', 'fault': 'Неисправен', 'unknown': 'Неопределен'}


def metadata(channels):
    r = channels.rename(columns={'ид_канала_данных': 'sensor_id'}).copy()
    r['object_key'] = r['ид_объект'].fillna('__missing__').astype(str)
    # Shared prefix is an electrical grouping hypothesis; its semantics are undocumented.
    r['loop_key'] = r.object_key + '|' + r['тег_инженерной_системы'].fillna('').str.rstrip('.').str.rsplit('.', n=1).str[0]
    parsed = []
    for name in r['название_датчика'].fillna(''):
        matches = list(re.finditer(r'ПК\s*(\d+(?:[.,]\d+)?)(?:\s*\+\s*(\d+(?:[.,]\d+)?))?', name, re.I))
        unambiguous = len(matches) == 1
        if unambiguous:
            tail = name[matches[0].end():]
            unambiguous = not re.match(r'\s*[-–]\s*(?:ПК\s*)?\d', tail, re.I)
            # Galleries and combined sensors cannot be located on one common axis safely.
            unambiguous &= re.search(r'(?:Г\s*\d|Гал\.|\d+\s*шт)', name, re.I) is None
        if unambiguous:
            a, b = matches[0].groups()
            parsed.append((float(a.replace(',', '.')), float(b.replace(',', '.')) if b else 0., 1.))
        else:
            parsed.append((np.nan, np.nan, 0.))
    r[META_NUM] = parsed
    assert not r.sensor_id.duplicated().any()
    return r.set_index('sensor_id')


def attach_metadata(rows, registry):
    f = rows.copy()
    for c in META_CATS + META_NUM:
        f[c] = f.sensor_id.map(registry[c])
    for c in META_CATS:
        f[c] = f[c].fillna('__missing__').astype(str)
    return f


def clean_events(events):
    return events.drop_duplicates(['sensor_id', 'event_id', 'ts', 'value']).sort_values(
        ['sensor_id', 'ts', 'event_id'], kind='stable')


def peer_maps(registry, sensor_ids):
    r = registry.loc[registry.index.intersection(sensor_ids)]
    groups = {}
    for scope, col in [('object', 'object_key'), ('loop', 'loop_key')]:
        d = {k: tuple(map(int, g.index)) for k, g in r.groupby(col)}
        groups[scope] = {int(sid): d[row[col]] for sid, row in r.iterrows()}
    # +/-2 in the printed primary picket number. This is NOT a distance in meters.
    # Plus offsets are retained as separate metadata, not assigned invented units.
    groups['near'] = {}
    for _, g in r.groupby('object_key'):
        for sid, row in g.iterrows():
            keep = g.picket_unambiguous.eq(1) & g.picket_number.sub(row.picket_number).abs().le(2)
            groups['near'][int(sid)] = tuple(map(int, g.index[keep])) if row.picket_unambiguous else ()
    return groups


def add_peer_features(rows, events, registry):
    """Use [T-window,T) events; exclude the queried channel from every peer count.

    Availability means a peer has an actual message in its last 90 days. It
    does not imply healthy status. Silence and absent observations stay distinct.
    """
    f = attach_metadata(rows, registry).reset_index(drop=True)
    ev = clean_events(events)
    seq = {}
    for sid, g in ev.groupby('sensor_id', sort=False):
        t = g.ts.to_numpy('datetime64[s]').astype('int64')
        seq[int(sid)] = {'events': t, **{k: t[g.value.eq(v)] for k, v in STATE.items()}}
    groups = peer_maps(registry, registry.index[registry['тип_датчика'].eq('Датчик дыма')])
    times = f.decision_time.to_numpy('datetime64[s]').astype('int64')
    ids = f.sensor_id.to_numpy()
    additions = {}
    for scope, mapping in groups.items():
        cols = [f'{scope}_peer_registry_count', f'{scope}_peer_seen90d']
        cols += [f'{scope}_{k}_{w}s_{v}' for k, ws in PEER_WINDOWS.items() for w in ws for v in ('count', 'distinct')]
        out = {c: np.zeros(len(f), dtype='float32') for c in cols}
        # Evaluate all queries sharing a peer set together, then remove their own counts.
        query_groups = {}
        for i, sid in enumerate(ids):
            members = mapping.get(int(sid), ())
            query_groups.setdefault(members, []).append(i)
        for members, ix in query_groups.items():
            ix = np.asarray(ix); t = times[ix]; query_ids = ids[ix]
            out[cols[0]][ix] = [len(members) - int(s in members) for s in query_ids]
            for peer in members:
                if peer not in seq:
                    continue
                other = query_ids != peer
                pt = seq[peer]['events']
                available = np.searchsorted(pt, t) - np.searchsorted(pt, t - 90*86400)
                out[cols[1]][ix] += (available > 0) & other
                for kind, windows in PEER_WINDOWS.items():
                    pt = seq[peer][kind]
                    stop = np.searchsorted(pt, t)
                    for w in windows:
                        counts = (stop - np.searchsorted(pt, t-w)) * other
                        out[f'{scope}_{kind}_{w}s_count'][ix] += counts
                        out[f'{scope}_{kind}_{w}s_distinct'][ix] += counts > 0
        for w in PEER_WINDOWS['smoke']:
            hits = out[f'{scope}_smoke_{w}s_distinct']
            observed = out[f'{scope}_peer_seen90d']
            out[f'{scope}_smoke_{w}s_fraction'] = np.divide(hits, observed, out=np.full(len(f), np.nan, dtype='float32'), where=observed > 0)
            out[f'{scope}_isolated_{w}s'] = np.where(observed > 0, hits == 0, np.nan).astype('float32')
        for c in list(out):
            if c.endswith('_count'):
                out[c] = np.log1p(out[c])
        additions.update(out)
    return pd.concat([f, pd.DataFrame(additions)], axis=1)


def add_own_sequence(rows, events):
    f = rows.reset_index(drop=True).copy()
    ev = clean_events(events)
    groups = {int(s): g for s, g in ev.groupby('sensor_id', sort=False)}
    cols = [f'own_{kind}_{w}s' for kind in ('flips', 'aba', 'fast_flips', 'duplicates') for w in (3600, 86400, 604800, 2592000)]
    out = {c: np.zeros(len(f), dtype='float32') for c in cols}
    out.update({c: np.full(len(f), np.nan, dtype='float32') for c in ['own_last_change_age_hours', 'own_last_completed_smoke_seconds']})
    for sid, q in f.groupby('sensor_id', sort=False):
        g = groups[int(sid)]; ts = g.ts.to_numpy('datetime64[s]').astype('int64'); v = g.value.to_numpy()
        t = q.decision_time.to_numpy('datetime64[s]').astype('int64'); ix = q.index.to_numpy()
        change = np.r_[False, v[1:] != v[:-1]]
        aba = np.r_[False, False, (v[2:] == v[:-2]) & (v[2:] != v[1:-1])] if len(v) > 1 else np.array([False])
        fast = change & (np.r_[np.inf, np.diff(ts)] <= 60)
        dup = ~change; dup[0] = False
        for kind, mask in [('flips', change), ('aba', aba), ('fast_flips', fast), ('duplicates', dup)]:
            st = ts[mask]; right = np.searchsorted(st, t)
            for w in (3600, 86400, 604800, 2592000):
                out[f'own_{kind}_{w}s'][ix] = np.log1p(right - np.searchsorted(st, t-w))
        st = ts[change]; p = np.searchsorted(st, t)-1
        if len(st): out['own_last_change_age_hours'][ix] = np.where(p >= 0, (t-st[np.maximum(p, 0)])/3600, np.nan)
        # Only completed smoke runs: never use the future end of the current state.
        run_starts = np.r_[0, np.flatnonzero(change)]
        a, b = run_starts[:-1], run_starts[1:]
        mask = v[a] == 'Обнаружен дым'; end = ts[b[mask]]; duration = ts[b[mask]]-ts[a[mask]]
        p = np.searchsorted(end, t)-1
        if len(end): out['own_last_completed_smoke_seconds'][ix] = np.where(p >= 0, np.log1p(duration[np.maximum(p, 0)]), np.nan)
    return pd.concat([f, pd.DataFrame(out)], axis=1)


def add_history(rows, history=None):
    """Past *observed* alarm outcomes, available only after the full 24h window.

    No model predictions are written back as facts. Also accumulate earlier
    isolated alarms even if their future fault outcome is not yet known.
    """
    f = rows.reset_index(drop=True).copy()
    h = (rows if history is None else history).copy()
    valid = h.target.notna()
    h = h.loc[valid].sort_values('decision_time')
    matured = (h.decision_time + pd.Timedelta(days=1)).to_numpy('datetime64[s]').astype('int64')
    t = f.decision_time.to_numpy('datetime64[s]').astype('int64')
    additions = {}
    for scope, key in [('self', 'sensor_id'), ('object', 'object_key'), ('loop', 'loop_key')]:
        cols = {f'history_{scope}_{name}_{w}d': np.zeros(len(f), dtype='float32') for name in ('n', 'rate') for w in (30, 90, 365, 3650)}
        hg = {k: g.index for k, g in h.reset_index(drop=True).groupby(key, sort=False)}
        hh = h.reset_index(drop=True)
        for k, q in f.groupby(key, sort=False):
            ix = q.index.to_numpy(); t0 = t[ix]
            hs = hg.get(k, [])
            if not len(hs):
                for w in (30, 90, 365, 3650): cols[f'history_{scope}_rate_{w}d'][ix] = .1
                continue
            st = matured[hs]; y = hh.target.to_numpy()[hs]; cum = np.r_[0., np.cumsum(y)]
            right = np.searchsorted(st, t0, side='right')
            for w in (30, 90, 365, 3650):
                left = np.searchsorted(st, t0-w*86400)
                n = right-left; pos = cum[right]-cum[left]
                # Fixed weak Beta prior; never estimated from the test labels.
                cols[f'history_{scope}_n_{w}d'][ix] = np.log1p(n)
                cols[f'history_{scope}_rate_{w}d'][ix] = (pos+.5)/(n+5.)
        additions.update(cols)
    # Temporal history of causal peer confirmation (excludes the current alarm).
    for scope in ('object', 'near', 'loop'):
        c = f'{scope}_isolated_3600s'
        for w in (7, 30, 90):
            additions[f'past_{scope}_isolated_rate_{w}d'] = np.full(len(f), np.nan, dtype='float32')
        for _, q in f.groupby('sensor_id', sort=False):
            ix = q.index.to_numpy(); tt = t[ix]; values = q[c].to_numpy()
            known = np.isfinite(values); c0 = np.r_[0, np.cumsum(known)]; c1 = np.r_[0, np.cumsum(np.nan_to_num(values))]
            right = np.searchsorted(tt, tt)
            for w in (7, 30, 90):
                left = np.searchsorted(tt, tt-w*86400); n = c0[right]-c0[left]
                additions[f'past_{scope}_isolated_rate_{w}d'][ix] = np.divide(c1[right]-c1[left], n, out=np.full(len(ix), np.nan), where=n>0)
    return pd.concat([f, pd.DataFrame(additions)], axis=1)


def build_extended(rows, events, channels):
    registry = metadata(channels)
    f = add_peer_features(rows, events, registry)
    f = add_own_sequence(f, events)
    f = add_history(f)
    excluded = {'decision_time', 'sensor_id', 'target', 'next_onset', 'lead_hours'}
    for c in f.columns:
        if c not in excluded and f[c].dtype.kind in 'fiu': f[c] = f[c].astype('float32')
    return f
