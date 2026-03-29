import matplotlib

matplotlib.use('Agg')
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np

# Try to use a clean sans-serif font
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['Helvetica Neue', 'Helvetica', 'Arial', 'sans-serif']

fig, axes = plt.subplots(1, 2, figsize=(10, 4.5), gridspec_kw={'wspace': 0.3})
fig.patch.set_facecolor('#f3f6fb')


def slice_xy(startangle, sizes, index, radius):
    total = sum(sizes)
    angle_before = sum(sizes[:index]) / total * 360
    angle_slice = sizes[index] / total * 360
    mid_deg = startangle - angle_before - angle_slice / 2
    mid_rad = np.radians(mid_deg)
    return radius * np.cos(mid_rad), radius * np.sin(mid_rad)


# ========== LEFT: Traditional Trading ==========
order_l = [40, 60]  # [Quant, Human]
cols_l = ['#d2daea', '#c0b0d8']

wedges_l, _ = axes[0].pie(
    order_l,
    colors=cols_l,
    startangle=90,
    counterclock=False,
    wedgeprops=dict(edgecolor='#f3f6fb', linewidth=3),
)

# Human text (index 1, the 60% slice) — nudge right
hx, hy = slice_xy(90, order_l, 1, 0.52)
axes[0].text(
    hx + 0.10,
    hy + 0.06,
    'Human',
    ha='center',
    va='center',
    fontsize=15,
    fontweight='bold',
    color='#3a2a5a',
)
axes[0].text(
    hx + 0.10,
    hy - 0.12,
    'design \u00b7 validate',
    ha='center',
    va='center',
    fontsize=9,
    color='#6a5a8a',
)
axes[0].text(
    hx + 0.10,
    hy - 0.26,
    'allocate \u00b7 monitor',
    ha='center',
    va='center',
    fontsize=9,
    color='#6a5a8a',
)

# Quant text (index 0, the 40% slice)
qx, qy = slice_xy(90, order_l, 0, 0.55)
axes[0].text(
    qx,
    qy + 0.05,
    'Algorithm',
    ha='center',
    va='center',
    fontsize=13,
    fontweight='bold',
    color='#415060',
)
axes[0].text(
    qx, qy - 0.12, 'execute', ha='center', va='center', fontsize=9, color='#647585'
)

# Title right below the pie
axes[0].text(
    0,
    -1.22,
    'Algorithmic Trading',
    ha='center',
    va='center',
    fontsize=13,
    fontweight='bold',
    color='#647585',
)

# ========== RIGHT: Agent-Native Trading ==========
order_r = [10, 33, 57]  # [Human(tiny), Quant, Agent(big)]
cols_r = ['#e8dff0', '#d2daea', '#18222e']

wedges_r, _ = axes[1].pie(
    order_r,
    colors=cols_r,
    startangle=90,
    counterclock=False,
    wedgeprops=dict(edgecolor='#f3f6fb', linewidth=3),
)

# Agent text (index 2, the 57% slice)
ax_, ay = slice_xy(90, order_r, 2, 0.50)
axes[1].text(
    ax_,
    ay + 0.06,
    'Agent',
    ha='center',
    va='center',
    fontsize=15,
    fontweight='bold',
    color='#f3f6fb',
)
axes[1].text(
    ax_,
    ay - 0.12,
    'discover \u00b7 validate',
    ha='center',
    va='center',
    fontsize=9,
    color='#8e9eae',
)
axes[1].text(
    ax_,
    ay - 0.26,
    'allocate \u00b7 deploy',
    ha='center',
    va='center',
    fontsize=9,
    color='#8e9eae',
)

# Quant text (index 1, the 33% slice)
qx2, qy2 = slice_xy(90, order_r, 1, 0.58)
axes[1].text(
    qx2,
    qy2 + 0.05,
    'Algorithm',
    ha='center',
    va='center',
    fontsize=13,
    fontweight='bold',
    color='#415060',
)
axes[1].text(
    qx2, qy2 - 0.12, 'execute', ha='center', va='center', fontsize=9, color='#647585'
)

# Human annotation (index 0, the 10% slice)
hpx, hpy = slice_xy(90, order_r, 0, 0.85)
axes[1].annotate(
    'Human: monitor',
    xy=(hpx, hpy),
    xytext=(hpx + 0.35, hpy + 0.35),
    fontsize=9,
    color='#647585',
    ha='center',
    fontweight='medium',
    arrowprops=dict(arrowstyle='->', color='#aab4c6', lw=1.2),
)

# Title right below the pie
axes[1].text(
    0,
    -1.22,
    'Agent-Native Trading',
    ha='center',
    va='center',
    fontsize=13,
    fontweight='bold',
    color='#18222e',
)

# Arrow between pies (drawn, not unicode)
ax_arrow = fig.add_axes([0.44, 0.46, 0.12, 0.08], frameon=False)
ax_arrow.set_xlim(0, 1)
ax_arrow.set_ylim(0, 1)
ax_arrow.set_xticks([])
ax_arrow.set_yticks([])
ax_arrow.annotate(
    '',
    xy=(0.95, 0.5),
    xytext=(0.05, 0.5),
    arrowprops=dict(arrowstyle='->', color='#aab4c6', lw=2.5),
)

for ax in axes:
    ax.set_aspect('equal')

plt.savefig(
    '/Users/yuhaofei/Downloads/prediction-market-cli/blog/trading_loop.png',
    dpi=200,
    bbox_inches='tight',
    facecolor='#f3f6fb',
    pad_inches=0.3,
)
print('OK')
