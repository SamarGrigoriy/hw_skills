/*
 * astar_core.c — ядро поиска пути трассировщика route.py.
 *
 * Почему на C. Поиск A* по сетке 0,1 мм — горячая точка трассировщика: на
 * Python один проход по плате 89 x 79 мм шёл 5 минут, а согласованной
 * перетрассировке нужны десятки проходов. Логика и стоимость пути — те же,
 * что в запасной Python-версии route.py (Router._astar_py): восемь направлений,
 * поворот на 45 градусов со штрафом, на 90 и больше — запрет, смена слоя —
 * переходное. Собирается gcc при первом запуске route.py; нет компилятора —
 * работает Python-версия, медленно, но так же.
 *
 * Сетка окна: индекс клетки = L*W*H + j*W + i, L — слой (0 F.Cu, 1 B.Cu).
 */
#include <math.h>
#include <stdlib.h>

typedef struct { double f; int idx; } HNode;

static HNode *heap = 0;
static long hsize = 0, hcap = 0;

static int hpush(double f, int idx) {
    if (hsize == hcap) {
        long nc = hcap ? hcap * 2 : 4096;
        HNode *nh = (HNode *)realloc(heap, nc * sizeof(HNode));
        if (!nh) return -1;
        heap = nh;
        hcap = nc;
    }
    long i = hsize++;
    while (i > 0) {
        long p = (i - 1) / 2;
        if (heap[p].f <= f) break;
        heap[i] = heap[p];
        i = p;
    }
    heap[i].f = f;
    heap[i].idx = idx;
    return 0;
}

static HNode hpop(void) {
    HNode top = heap[0];
    HNode last = heap[--hsize];
    long i = 0;
    for (;;) {
        long l = 2 * i + 1;
        if (l >= hsize) break;
        long r = l + 1;
        long m = (r < hsize && heap[r].f < heap[l].f) ? r : l;
        if (heap[m].f >= last.f) break;
        heap[i] = heap[m];
        i = m;
    }
    if (hsize > 0) heap[i] = last;
    return top;
}

static const int DI[8] = {1, 1, 0, -1, -1, -1, 0, 1};
static const int DJ[8] = {0, 1, 1, 1, 0, -1, -1, -1};

static double octile(int i, int j, int tx, int ty) {
    int dx = abs(i - tx), dy = abs(j - ty);
    int mx = dx > dy ? dx : dy, mn = dx > dy ? dy : dx;
    return mx + 0.41421356 * mn;
}

/* Возвращает длину пути в клетках (путь в out_path от старта к цели) или -1. */
int astar(int W, int H,
          const unsigned char *blocked,   /* 2*W*H: 1 — клетка закрыта для цепи */
          const unsigned char *vblock,    /* W*H:   1 — переходное нельзя */
          const float *soft,              /* 2*W*H: добавочная стоимость клетки, или NULL */
          const unsigned char *tmask,     /* 2*W*H: 1 — цель */
          const int *starts, int nstarts,
          int tx, int ty,
          double cost_bottom, double cost_via, double cost_turn45,
          long limit,
          int *out_path, int out_cap,
          double *best, int *parent, unsigned char *pdir) {
    long WH = (long)W * H, N = 2 * WH;
    for (long k = 0; k < N; k++) { best[k] = INFINITY; parent[k] = -1; pdir[k] = 8; }
    hsize = 0;
    for (int s = 0; s < nstarts; s++) {
        int idx = starts[s];
        if (idx < 0 || idx >= N) continue;
        best[idx] = 0.0;
        long rem = idx % WH;
        if (hpush(octile((int)(rem % W), (int)(rem / W), tx, ty), idx)) return -2;
    }
    long visited = 0;
    int found = -1;
    while (hsize > 0) {
        HNode nd = hpop();
        int idx = nd.idx;
        int L = (int)(idx / WH);
        long rem = idx - L * WH;
        int j = (int)(rem / W), i = (int)(rem % W);
        double g = best[idx];
        if (nd.f > g + octile(i, j, tx, ty) + 1e-6) continue;
        if (tmask[idx]) { found = idx; break; }
        if (++visited > limit) break;
        int pd = pdir[idx];
        double lc = L ? cost_bottom : 1.0;
        for (int d = 0; d < 8; d++) {
            int turn = 0;
            if (pd != 8) {
                int t = (d - pd + 8) % 8;
                turn = t < 8 - t ? t : 8 - t;
                if (turn >= 2) continue;
            }
            int ni = i + DI[d], nj = j + DJ[d];
            if (ni < 0 || nj < 0 || ni >= W || nj >= H) continue;
            long nidx = L * WH + (long)nj * W + ni;
            if (blocked[nidx]) continue;
            double st = (DI[d] && DJ[d]) ? 1.41421356 : 1.0;
            double ng = g + st * lc + (turn == 1 ? cost_turn45 : 0.0);
            if (soft) ng += st * soft[nidx];
            if (ng < best[nidx] - 1e-9) {
                best[nidx] = ng;
                parent[nidx] = idx;
                pdir[nidx] = (unsigned char)d;
                if (hpush(ng + octile(ni, nj, tx, ty), (int)nidx)) return -2;
            }
        }
        if (!vblock[rem]) {
            long nidx = (1 - L) * WH + rem;
            double ng = g + cost_via + (soft ? soft[nidx] : 0.0);
            if (ng < best[nidx] - 1e-9) {
                best[nidx] = ng;
                parent[nidx] = idx;
                pdir[nidx] = 8;
                if (hpush(ng + octile(i, j, tx, ty), (int)nidx)) return -2;
            }
        }
    }
    if (found < 0) return -1;
    int n = 0;
    for (int k = found; k >= 0; k = parent[k]) {
        if (n >= out_cap) return -3;
        out_path[n++] = k;
    }
    for (int a = 0, b = n - 1; a < b; a++, b--) {
        int t = out_path[a]; out_path[a] = out_path[b]; out_path[b] = t;
    }
    return n;
}
