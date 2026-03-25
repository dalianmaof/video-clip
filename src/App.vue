<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, type Ref } from 'vue'
import { open } from '@tauri-apps/plugin-dialog'
import { readTextFile, writeTextFile } from '@tauri-apps/plugin-fs'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { resolveResource, appConfigDir } from '@tauri-apps/api/path'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'

// ── Types ──────────────────────────────────────────────────────────────────
interface AppConfig {
  input_dir: string; output_dir: string; input_ext: string
  workers: number; encode_mode: string
  shard_index: number | null; shard_count: number | null
  theme?: 'light' | 'dark'
}
interface StatusResult {
  pending: number; processing: number; done: number; failed: number
  total_done_seconds: number
  failed_files: { input_path: string; error_msg: string }[]
}
interface ProgressPayload {
  processed: number; total: number; status: string
  file_name: string; duration_s: number; elapsed_s: number
}

// ── State ──────────────────────────────────────────────────────────────────
const inputDir = ref('')
const outputDir = ref('')
const inputExt = ref('.ts,.mp4,.mkv,.avi,.mov')
const workers = ref(4)
const encodeMode = ref('quality')
const shardEnabled = ref(false)
const shardIndex = ref(0)
const shardCount = ref(10)
const hasNvenc = ref(false)
const isDark = ref(false)

function applyTheme(dark: boolean) {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
}
function toggleTheme() {
  applyTheme(!isDark.value)
  saveConfig()
}

const isRunning = ref(false)
const statusText = ref('就绪')
const logLines = ref<string[]>([])
const logEl = ref<HTMLElement | null>(null)
const stats = reactive({ done: 0, failed: 0 })
const totalFiles = ref(0)
const currentFileName = ref('')
const currentFileDuration = ref(0)
const elapsedSeconds = ref(0)
let elapsedTimer: ReturnType<typeof setInterval> | null = null
let startTimestamp = 0
let isMounted = true
const unlisteners: UnlistenFn[] = []

// ── Helpers ────────────────────────────────────────────────────────────────
function formatDuration(seconds: number): string {
  if (seconds <= 0) return '00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

// ── Config ─────────────────────────────────────────────────────────────────
async function getConfigPath(): Promise<string> {
  try { return await resolveResource('config.json') }
  catch { return (await appConfigDir()) + 'config.json' }
}
async function loadConfig() {
  try {
    const cfg: Partial<AppConfig> = JSON.parse(await readTextFile(await getConfigPath()))
    if (cfg.input_dir) inputDir.value = cfg.input_dir
    if (cfg.output_dir) outputDir.value = cfg.output_dir
    if (cfg.input_ext) inputExt.value = cfg.input_ext
    if (cfg.workers) workers.value = cfg.workers
    if (cfg.encode_mode) encodeMode.value = cfg.encode_mode
    if (cfg.shard_index != null) { shardEnabled.value = true; shardIndex.value = cfg.shard_index }
    if (cfg.shard_count != null) shardCount.value = cfg.shard_count
    if (cfg.theme) applyTheme(cfg.theme === 'dark')
  } catch (e) { console.warn('loadConfig:', e) }
}
async function saveConfig() {
  try {
    await writeTextFile(await getConfigPath(), JSON.stringify({
      input_dir: inputDir.value, output_dir: outputDir.value, input_ext: inputExt.value,
      workers: workers.value, encode_mode: encodeMode.value,
      shard_index: shardEnabled.value ? shardIndex.value : null,
      shard_count: shardEnabled.value ? shardCount.value : null,
      theme: isDark.value ? 'dark' : 'light',
    }, null, 2))
  } catch (e) { console.warn('saveConfig:', e) }
}

// ── NVENC ──────────────────────────────────────────────────────────────────
async function detectNvenc() {
  try {
    hasNvenc.value = await invoke<boolean>('detect_nvenc')
    appendLog(hasNvenc.value ? '[INFO] NVENC GPU 检测成功' : '[INFO] NVENC 不可用，仅 CPU 编码')
  } catch (err: any) { hasNvenc.value = false; appendLog(`[WARN] NVENC 检测失败: ${err}`) }
  if (!hasNvenc.value && encodeMode.value !== 'quality') encodeMode.value = 'quality'
}

// ── Folder picker ──────────────────────────────────────────────────────────
async function pickDir(target: Ref<string>) {
  const s = await open({ directory: true, multiple: false })
  if (typeof s === 'string') target.value = s
}
const pickInput = () => pickDir(inputDir)
const pickOutput = () => pickDir(outputDir)

// ── Log ────────────────────────────────────────────────────────────────────
function appendLog(line: string) {
  logLines.value.push(line)
  if (logLines.value.length > 5500) logLines.value = logLines.value.slice(500)
  nextTick(() => { if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight })
}

// ── Progress ───────────────────────────────────────────────────────────────
const pending = computed(() => Math.max(0, totalFiles.value - stats.done - stats.failed))
const progressPct = computed(() => totalFiles.value ? Math.round((stats.done + stats.failed) / totalFiles.value * 100) : 0)

const animatedPct = ref(0)
let animFrame: number | null = null
function tickAnimation() {
  if (!isMounted) { animFrame = null; return }
  const diff = progressPct.value - animatedPct.value
  if (Math.abs(diff) < 0.5) { animatedPct.value = progressPct.value; animFrame = null; return }
  animatedPct.value += diff * 0.12
  animFrame = requestAnimationFrame(tickAnimation)
}
watch(progressPct, () => { if (!animFrame) animFrame = requestAnimationFrame(tickAnimation) })
const displayPct = computed(() => Math.round(animatedPct.value))

const eta = computed(() => {
  const done = stats.done + stats.failed
  if (!done || !totalFiles.value || elapsedSeconds.value <= 0) return ''
  return formatDuration(Math.round((elapsedSeconds.value / done) * (totalFiles.value - done)))
})
const elapsedDisplay = computed(() => formatDuration(elapsedSeconds.value))

function startTimer() { stopTimer(); startTimestamp = Date.now(); elapsedTimer = setInterval(() => { elapsedSeconds.value = Math.floor((Date.now() - startTimestamp) / 1000) }, 1000) }
function stopTimer() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null } }

// ── Actions ────────────────────────────────────────────────────────────────
async function onStart() {
  if (!inputDir.value || !outputDir.value) { appendLog('[错误] 请先选择输入和输出文件夹'); return }
  await saveConfig()
  logLines.value = []; stats.done = 0; stats.failed = 0
  totalFiles.value = 0; currentFileName.value = ''; elapsedSeconds.value = 0; animatedPct.value = 0
  isRunning.value = true; statusText.value = '处理中'; startTimer()
  try {
    await invoke('start_batch', { config: {
      input_dir: inputDir.value, output_dir: outputDir.value, input_ext: inputExt.value,
      workers: workers.value, encode_mode: encodeMode.value,
      shard_index: shardEnabled.value ? shardIndex.value : null,
      shard_count: shardEnabled.value ? shardCount.value : null,
    }})
  } catch (err: any) { isRunning.value = false; statusText.value = '出错'; stopTimer(); appendLog(`[ERROR] ${err}`) }
}
async function onStop() { statusText.value = '停止中...'; await invoke('stop_batch') }
async function onStatus() {
  if (!outputDir.value) { appendLog('[错误] 请先设置输出文件夹'); return }
  try {
    const r = await invoke<StatusResult>('query_status', { query: { output_dir: outputDir.value } })
    appendLog(`--- 状态: 等待 ${r.pending}  处理中 ${r.processing}  完成 ${r.done}  失败 ${r.failed} ---`)
    for (const f of r.failed_files) appendLog(`  [FAIL] ${f.input_path}: ${f.error_msg}`)
  } catch (err: any) { appendLog(`[ERROR] ${err}`) }
}

// ── Events ─────────────────────────────────────────────────────────────────
async function setupListeners() {
  unlisteners.push(await listen<{ message: string }>('batch:log', e => appendLog(e.payload.message)))
  unlisteners.push(await listen<ProgressPayload>('batch:progress', e => {
    const { total, status, file_name, duration_s, elapsed_s } = e.payload
    if (totalFiles.value !== total) totalFiles.value = total
    if (status === 'done') stats.done++; else if (status === 'failed') stats.failed++
    if (file_name) { currentFileName.value = file_name; currentFileDuration.value = duration_s }
    if (elapsed_s > 0) elapsedSeconds.value = Math.floor(elapsed_s)
  }))
  unlisteners.push(await listen<{ success: boolean; done: number; failed: number; elapsed_s: number }>('batch:finished', e => {
    isRunning.value = false; stopTimer()
    stats.done = e.payload.done; stats.failed = e.payload.failed
    elapsedSeconds.value = Math.floor(e.payload.elapsed_s); currentFileName.value = ''
    statusText.value = e.payload.done === 0 && e.payload.failed === 0 ? '就绪' : e.payload.success ? '完成' : '有失败'
    if (e.payload.success && e.payload.done > 0) appendLog('\n[完成] 所有任务已处理完毕。')
    else if (e.payload.failed > 0) appendLog(`\n[结束] 完成 ${e.payload.done}  失败 ${e.payload.failed}`)
  }))
}

onMounted(async () => { await setupListeners(); await loadConfig(); await detectNvenc() })
onUnmounted(() => { isMounted = false; stopTimer(); if (animFrame) cancelAnimationFrame(animFrame); for (const u of unlisteners) u() })
</script>

<template>
  <div class="h-screen flex flex-col bg-background text-foreground select-none">

    <!-- Sidebar-style header -->
    <div class="flex items-center justify-between px-4 h-11 border-b bg-card shrink-0">
      <div class="flex items-center gap-2.5">
        <div class="w-5 h-5 rounded bg-primary/90 flex items-center justify-center">
          <svg class="w-3 h-3 text-primary-foreground" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </div>
        <span class="text-[13px] font-medium">视频批量处理器</span>
      </div>
      <div class="flex items-center gap-3">
        <!-- Theme toggle -->
        <button @click="toggleTheme" class="w-7 h-7 rounded-md flex items-center justify-center hover:bg-accent transition-colors cursor-pointer" title="切换主题">
          <!-- Sun (shown in dark mode) -->
          <svg v-if="isDark" class="w-3.5 h-3.5 text-muted-foreground" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
          </svg>
          <!-- Moon (shown in light mode) -->
          <svg v-else class="w-3.5 h-3.5 text-muted-foreground" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
          </svg>
        </button>
        <!-- Status indicator -->
        <div class="flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full" :class="isRunning ? 'bg-primary animate-pulse' : hasNvenc ? 'bg-success' : 'bg-muted-foreground/30'"></span>
          <span class="text-[11px] text-muted-foreground">{{ isRunning ? statusText : hasNvenc ? 'GPU 就绪' : 'CPU 模式' }}</span>
        </div>
      </div>
    </div>

    <!-- Main content -->
    <div class="flex-1 overflow-auto">
      <div class="p-4 space-y-3 max-w-full">

        <!-- Row 1: Input/Output -->
        <div class="rounded-md border bg-card p-3 space-y-2">
          <div class="flex items-center gap-2">
            <span class="text-[11px] text-muted-foreground w-10 shrink-0">输入</span>
            <Input :model-value="inputDir" placeholder="选择源视频文件夹" readonly class="flex-1 h-7 text-[12px] bg-secondary/50 border-0 focus-visible:ring-1" />
            <Button variant="secondary" size="sm" class="h-7 text-[11px] px-3 cursor-pointer" @click="pickInput">浏览</Button>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-[11px] text-muted-foreground w-10 shrink-0">输出</span>
            <Input :model-value="outputDir" placeholder="选择输出目录" readonly class="flex-1 h-7 text-[12px] bg-secondary/50 border-0 focus-visible:ring-1" />
            <Button variant="secondary" size="sm" class="h-7 text-[11px] px-3 cursor-pointer" @click="pickOutput">浏览</Button>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-[11px] text-muted-foreground w-10 shrink-0">格式</span>
            <Input v-model="inputExt" class="w-48 h-7 text-[12px] bg-secondary/50 border-0 focus-visible:ring-1" />
          </div>
        </div>

        <!-- Row 2: Options -->
        <div class="rounded-md border bg-card p-3">
          <div class="flex items-center gap-4 flex-wrap">
            <div class="flex items-center gap-2">
              <span class="text-[11px] text-muted-foreground">并行</span>
              <Input v-model.number="workers" type="number" :min="1" :max="32" class="w-14 h-7 text-[12px] bg-secondary/50 border-0 focus-visible:ring-1 text-center" />
            </div>
            <div class="flex items-center gap-2">
              <span class="text-[11px] text-muted-foreground">画质</span>
              <Select v-model="encodeMode">
                <SelectTrigger class="w-44 h-7 text-[12px] bg-secondary/50 border-0 focus-visible:ring-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="quality">高画质 (CPU)</SelectItem>
                  <SelectItem value="gpu_quality" :disabled="!hasNvenc">中画质 (GPU){{ !hasNvenc ? ' — 不可用' : '' }}</SelectItem>
                  <SelectItem value="fast" :disabled="!hasNvenc">低画质 (GPU 极速){{ !hasNvenc ? ' — 不可用' : '' }}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div class="h-4 w-px bg-border"></div>
            <div class="flex items-center gap-2">
              <Checkbox id="shard" :checked="shardEnabled" @update:checked="(v: boolean) => shardEnabled = v" />
              <label for="shard" class="text-[11px] cursor-pointer">分片</label>
              <template v-if="shardEnabled">
                <Input v-model.number="shardIndex" type="number" :min="0" :max="99" class="w-12 h-6 text-[11px] bg-secondary/50 border-0 text-center" />
                <span class="text-[11px] text-muted-foreground">/</span>
                <Input v-model.number="shardCount" type="number" :min="2" :max="100" class="w-12 h-6 text-[11px] bg-secondary/50 border-0 text-center" />
              </template>
            </div>
          </div>
        </div>

        <!-- Row 3: Progress -->
        <div class="rounded-md border bg-card p-3 space-y-2.5">
          <div class="flex items-center justify-between">
            <div class="flex items-baseline gap-2">
              <span class="text-xl font-semibold tabular-nums">{{ displayPct }}%</span>
              <span v-if="totalFiles" class="text-[11px] text-muted-foreground tabular-nums">{{ stats.done + stats.failed }} / {{ totalFiles }}</span>
            </div>
            <div class="flex items-center gap-3 text-[11px] tabular-nums text-muted-foreground">
              <span v-if="elapsedSeconds > 0">{{ elapsedDisplay }}</span>
              <span v-if="isRunning && eta" class="text-primary font-medium">ETA {{ eta }}</span>
            </div>
          </div>

          <!-- Bar -->
          <div class="h-1.5 w-full rounded-full bg-secondary overflow-hidden">
            <div class="h-full rounded-full bg-primary transition-none" :style="{ width: `${animatedPct}%` }" />
          </div>

          <!-- Info row -->
          <div class="flex items-center justify-between text-[11px]">
            <div v-if="currentFileName" class="flex items-center gap-1.5 text-muted-foreground min-w-0 mr-4">
              <span v-if="isRunning" class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse shrink-0"></span>
              <span class="truncate">{{ currentFileName }}</span>
              <span v-if="currentFileDuration > 0" class="tabular-nums shrink-0">{{ currentFileDuration.toFixed(1) }}s</span>
            </div>
            <div class="flex items-center gap-3 shrink-0">
              <span class="text-success tabular-nums">{{ stats.done }} 完成</span>
              <span :class="stats.failed > 0 ? 'text-destructive' : 'text-muted-foreground/50'" class="tabular-nums">{{ stats.failed }} 失败</span>
              <span class="text-muted-foreground/60 tabular-nums">{{ pending }} 等待</span>
            </div>
          </div>
        </div>

        <!-- Row 4: Log -->
        <div class="rounded-md border bg-card overflow-hidden">
          <div class="px-3 py-1.5 border-b bg-secondary/30">
            <span class="text-[11px] text-muted-foreground">日志输出</span>
          </div>
          <pre
            ref="logEl"
            class="h-40 overflow-y-auto px-3 py-2 text-[11px] font-mono leading-relaxed whitespace-pre-wrap break-all text-muted-foreground/80"
          >{{ logLines.join('\n') }}</pre>
        </div>

      </div>
    </div>

    <!-- Bottom toolbar -->
    <div class="flex items-center gap-2 px-4 h-10 border-t bg-card shrink-0">
      <Button size="sm" class="h-7 px-4 text-[11px] cursor-pointer" @click="onStart" :disabled="isRunning">
        <svg v-if="!isRunning" class="w-3 h-3 mr-1" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        <svg v-else class="w-3 h-3 mr-1 animate-spin" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
        {{ isRunning ? '处理中...' : '开始处理' }}
      </Button>
      <Button variant="secondary" size="sm" class="h-7 text-[11px] cursor-pointer" @click="onStop" :disabled="!isRunning">停止</Button>
      <Button variant="secondary" size="sm" class="h-7 text-[11px] cursor-pointer" @click="onStatus" :disabled="isRunning">查询状态</Button>
      <span class="ml-auto text-[11px] text-muted-foreground tabular-nums">{{ statusText }}</span>
    </div>

  </div>
</template>
