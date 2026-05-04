import json
from collections import Counter
import duckdb
import matplotlib.pyplot as plt
import pandas as pd

con = duckdb.connect()

opened = con.execute("""
    SELECT DATE_TRUNC('month', createdAt::TIMESTAMP) AS month, COUNT(*) AS opened
    FROM read_json('godot_prs_all_12mo.json')
    GROUP BY month ORDER BY month
""").df()

merged = con.execute("""
    SELECT DATE_TRUNC('month', closedAt::TIMESTAMP) AS month, COUNT(*) AS merged
    FROM read_json('godot_prs_closed_12mo.json')
    WHERE state = 'MERGED'
    GROUP BY month ORDER BY month
""").df()

closed_unmerged = con.execute("""
    SELECT DATE_TRUNC('month', closedAt::TIMESTAMP) AS month, COUNT(*) AS closed_unmerged
    FROM read_json('godot_prs_closed_12mo.json')
    WHERE state = 'CLOSED'
    GROUP BY month ORDER BY month
""").df()

BASELINE_OPEN = 3590  # open PRs as of 2025-05-02 via GitHub search API
df = pd.merge(opened, merged, on='month', how='outer')
df = pd.merge(df, closed_unmerged, on='month', how='outer').fillna(0).sort_values('month')
df['net'] = df['opened'] - df['merged'] - df['closed_unmerged']
df['total_open'] = BASELINE_OPEN + df['net'].cumsum()

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)

ax1.plot(df['month'], df['opened'], marker='o', label='Opened')
ax1.plot(df['month'], df['merged'], marker='o', label='Merged')
ax1.fill_between(df['month'], df['opened'], alpha=0.15)
ax1.fill_between(df['month'], df['merged'], alpha=0.15)
ax1.set_ylabel('PRs per Month')
ax1.set_title('Godot PRs Opened vs Merged per Month')
ax1.legend()

ax2.plot(df['month'], df['total_open'], marker='o', color='purple')
ax2.fill_between(df['month'], df['total_open'], alpha=0.15, color='purple')
ax2.set_ylabel('Total Open PRs')
ax2.set_title('Total Open PRs Over Time')
ax2.set_xlabel('Month')
plt.xticks(rotation=45, ha='right')

plt.tight_layout()
plt.savefig('prs_over_time.png', dpi=150)
plt.show()

# --- Line graph: % of open PRs reviewed over time (by creation month) ---
open_prs = json.load(open('godot_prs_open_reviews.json'))
monthly = {}
for pr in open_prs:
    month = pr['createdAt'][:7]
    reviewed = len(pr['latestReviews']) > 0
    if month not in monthly:
        monthly[month] = {'total': 0, 'reviewed': 0}
    monthly[month]['total'] += 1
    if reviewed:
        monthly[month]['reviewed'] += 1

review_df = pd.DataFrame([
    {'month': pd.Timestamp(m + '-01'),
     'pct_reviewed': 100 * v['reviewed'] / v['total'],
     'total': v['total'],
     'reviewed': v['reviewed']}
    for m, v in sorted(monthly.items())
    if v['total'] >= 3  # skip months with too few PRs for meaningful %
])

fig3, ax = plt.subplots(figsize=(14, 5))
ax.plot(review_df['month'], review_df['pct_reviewed'], marker='o', color='green')
ax.fill_between(review_df['month'], review_df['pct_reviewed'], alpha=0.15, color='green')
ax.set_ylabel('% Reviewed')
ax.set_xlabel('Month PR was created')
ax.set_title('% of Currently Open PRs That Have Been Reviewed (by creation month)')
ax.set_ylim(0, 100)
plt.xticks(rotation=45, ha='right')
plt.tight_layout()
plt.savefig('pr_review_rate.png', dpi=150)
plt.show()

# --- Well-liked vs Controversial scatter ---
hyped = json.load(open('godot_prs_hyped.json'))

positive_keys = ('+1', 'hooray', 'heart', 'rocket')
negative_keys = ('-1', 'confused')

for p in hyped:
    r = p['reactions']
    p['positive'] = sum(r[k] for k in positive_keys)
    p['negative'] = sum(r[k] for k in negative_keys)
    p['controversy'] = p['comments'] / (p['positive'] + 1)

fig4, ax = plt.subplots(figsize=(14, 10))
scatter = ax.scatter(
    [p['positive'] for p in hyped],
    [p['comments'] for p in hyped],
    c=[p['controversy'] for p in hyped],
    cmap='RdYlGn_r',
    s=80,
    alpha=0.8
)
plt.colorbar(scatter, ax=ax, label='Controversy ratio (comments / positive reactions)')

# Label notable points
for p in hyped:
    if p['positive'] > 150 or p['comments'] > 100 or p['controversy'] > 1.5:
        label = f"#{p['number']} {p['title'][:30]}…"
        ax.annotate(label, (p['positive'], p['comments']),
                    fontsize=7, alpha=0.85,
                    xytext=(5, 3), textcoords='offset points')

ax.set_xlabel('Positive Reactions (+1, hooray, heart, rocket)')
ax.set_ylabel('Comments')
ax.set_title('Open Godot PRs: Well-Liked (green, bottom-right) vs Controversial (red, top-left)')
plt.tight_layout()
plt.savefig('pr_hyped.png', dpi=150)
plt.show()

# --- Pie chart: PR type labels only ---
prs = json.load(open('godot_prs_all_12mo.json'))

def is_type_label(name):
    return not any(name.startswith(p) for p in ('topic:', 'platform:', 'cherrypick:'))

type_counts = Counter(
    label['name']
    for pr in prs
    for label in pr['labels']
    if is_type_label(label['name'])
)
labels, counts = zip(*type_counts.most_common())

fig2, ax = plt.subplots(figsize=(10, 10))
ax.pie(counts, labels=labels, autopct='%1.1f%%', startangle=140)
ax.set_title('PR Type Label Distribution (past 12 months)')
plt.tight_layout()
plt.savefig('pr_labels_pie.png', dpi=150)
plt.show()
