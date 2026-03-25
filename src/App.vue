<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { open } from '@tauri-apps/plugin-dialog'
import { readTextFile, writeTextFile, exists } from '@tauri-apps/plugin-fs'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { resolveResource, appConfigDir } from '@tauri-apps/api/path'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Progress } from '@/components/ui/progress'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

// ── Types ──────────────────────────────────────────────────────────────────
interface AppConfig {
  input_dir: string
  output_dir: string
  input_ext: string
  workers: number
  encode_mode: string
  shard_index: number | null
  shard_count: number | null
}

interface Stats {
  pending: number
  done: number
  failed: number
}

interface StatusResult {
  pending: number
  processing: number
  done: number
  failed: number
  total_done_seconds: number
  failed_files: { input_path: string; error_msg: string }[]
}

interface ProgressPayload {
  processed: number
  total: number
  status: string
  file_name: string
  duration_s: number
  elapsed_s: number
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

const isRunning = ref(false)
const statusText = ref('就绪')
const logLines = ref<string[]>([])
const logEl = ref<HTMLElement | null>(null)
const stats = reactive<Stats>({ pending: 0, done: 0, failed: 0 })
const totalFiles = ref(0)

// Progress enhancements
const currentFileName = ref('')
const currentFileDuration = ref(0)
const elapsedSeconds = ref(0)
let elapsedTimer: ReturnType<typeof setInterval> | null = null
let startTimestamp = 0

const unlisteners: UnlistenFn[] = []

// ── Helpers ────────────────────────────────────────────────────────────────
function formatDuration(seconds: number): string {
  if (seconds <= 0) return '00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (h > 0) return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

// ── Config path ────────────────────────────────────────────────────────────
async function getConfigPath(): Promise<string> {
  try {
    return await resolveResource('config.json')
  } catch {
    const dir = await appConfigDir()
    return dir + 'config.json'
  }
}

// ── Load / Save config ─────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const path = await getConfigPath()
    const ok = await exists(path)
    if (!ok) return
    const text = await readTextFile(path)
    const cfg: Partial<AppConfig> = JSON.parse(text)
    if (cfg.input_dir)  inputDir.value  = cfg.input_dir
    if (cfg.output_dir) outputDir.value = cfg.output_dir
    if (cfg.input_ext)  inputExt.value  = cfg.input_ext
    if (cfg.workers)    workers.value   = cfg.workers
    if (cfg.encode_mode) encodeMode.value = cfg.encode_mode
    if (cfg.shard_index != null) {
      shardEnabled.value = true
      shardIndex.value = cfg.shard_index
    }
    if (cfg.shard_count != null) shardCount.value = cfg.shard_count
  } catch (e) {
    console.warn('Failed to load config:', e)
  }
}

async function saveConfig() {
  try {
    const path = await getConfigPath()
    const cfg: AppConfig = {
      input_dir:   inputDir.value,
      output_dir:  outputDir.value,
      input_ext:   inputExt.value,
      workers:     workers.value,
      encode_mode: encodeMode.value,
      shard_index: shardEnabled.value ? shardIndex.value : null,
      shard_count: shardEnabled.value ? shardCount.value : null,
    }
    await writeTextFile(path, JSON.stringify(cfg, null, 2))
  } catch (e) {
    console.warn('Failed to save config:', e)
  }
}

// ── NVENC detection ────────────────────────────────────────────────────────
async function detectNvenc() {
  try {
    hasNvenc.value = await invoke<boolean>('detect_nvenc')
    appendLog(hasNvenc.value
      ? '[INFO] NVENC GPU 检测成功'
      : '[INFO] NVENC 不可用，仅支持 CPU 编码')
  } catch (err: any) {
    hasNvenc.value = false
    appendLog(`[WARN] NVENC 检测失败: ${err}`)
  }
  if (!hasNvenc.value && encodeMode.value !== 'quality') {
    encodeMode.value = 'quality'
  }
}

// ── Folder picker ──────────────────────────────────────────────────────────
async function pickInput() {
  const selected = await open({ directory: true, multiple: false })
  if (typeof selected === 'string') inputDir.value = selected
}

async function pickOutput() {
  const selected = await open({ directory: true, multiple: false })
  if (typeof selected === 'string') outputDir.value = selected
}

// ── Log helpers ────────────────────────────────────────────────────────────
function appendLog(line: string) {
  logLines.value.push(line)
  if (logLines.value.length > 5000) logLines.value.shift()
  nextTick(() => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
  })
}

// ── Progress computations ─────────────────────────────────────────────────
const progressPct = computed(() => {
  if (!totalFiles.value) return 0
  return Math.round((stats.done + stats.failed) / totalFiles.value * 100)
})

// Animated progress: smoothly interpolates to target value
const animatedPct = ref(0)
let animationFrame: number | null = null

function animateProgress() {
  const target = progressPct.value
  const diff = target - animatedPct.value
  if (Math.abs(diff) < 0.5) {
    animatedPct.value = target
    animationFrame = null
    return
  }
  // Ease toward target: faster when far, slower when close
  animatedPct.value += diff * 0.15
  animationFrame = requestAnimationFrame(animateProgress)
}

// Watch progressPct and kick off animation
watch(progressPct, () => {
  if (!animationFrame) {
    animationFrame = requestAnimationFrame(animateProgress)
  }
})

const displayPct = computed(() => Math.round(animatedPct.value))

const eta = computed(() => {
  const processed = stats.done + stats.failed
  if (processed === 0 || !totalFiles.value || elapsedSeconds.value <= 0) return ''
  const remaining = totalFiles.value - processed
  const avgPerFile = elapsedSeconds.value / processed
  return formatDuration(Math.round(avgPerFile * remaining))
})

const elapsedDisplay = computed(() => formatDuration(elapsedSeconds.value))

function startElapsedTimer() {
  startTimestamp = Date.now()
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value = Math.floor((Date.now() - startTimestamp) / 1000)
  }, 1000)
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer)
    elapsedTimer = null
  }
}

// ── Start processing ───────────────────────────────────────────────────────
async function onStart() {
  if (!inputDir.value || !outputDir.value) {
    appendLog('[错误] 请先选择输入和输出文件夹')
    return
  }
  await saveConfig()

  logLines.value = []
  stats.pending = 0
  stats.done    = 0
  stats.failed  = 0
  totalFiles.value = 0
  currentFileName.value = ''
  currentFileDuration.value = 0
  elapsedSeconds.value = 0
  animatedPct.value = 0
  isRunning.value  = true
  statusText.value = '正在处理...'
  startElapsedTimer()

  try {
    await invoke('start_batch', {
      config: {
        input_dir:   inputDir.value,
        output_dir:  outputDir.value,
        input_ext:   inputExt.value,
        workers:     workers.value,
        encode_mode: encodeMode.value,
        shard_index: shardEnabled.value ? shardIndex.value : null,
        shard_count: shardEnabled.value ? shardCount.value : null,
      }
    })
  } catch (err: any) {
    isRunning.value = false
    statusText.value = '处理出错'
    stopElapsedTimer()
    appendLog(`[ERROR] ${err}`)
  }
}

// ── Stop ───────────────────────────────────────────────────────────────────
async function onStop() {
  statusText.value = '正在停止...'
  await invoke('stop_batch')
}

// ── Query status ───────────────────────────────────────────────────────────
async function onStatus() {
  if (!outputDir.value) {
    appendLog('[错误] 请先设置输出文件夹')
    return
  }
  appendLog('--- 状态查询 ---')
  try {
    const result = await invoke<StatusResult>('query_status', {
      query: { output_dir: outputDir.value }
    })
    appendLog(`  pending:    ${result.pending}`)
    appendLog(`  processing: ${result.processing}`)
    appendLog(`  done:       ${result.done}`)
    appendLog(`  failed:     ${result.failed}`)
    if (result.failed_files.length > 0) {
      appendLog(`失败文件 (${result.failed_files.length}):`)
      for (const f of result.failed_files) {
        appendLog(`  ${f.input_path}`)
        appendLog(`    原因: ${f.error_msg}`)
      }
    }
  } catch (err: any) {
    appendLog(`[ERROR] ${err}`)
  }
  appendLog('--- 查询完毕 ---')
}

// ── Event listeners ────────────────────────────────────────────────────────
async function setupListeners() {
  unlisteners.push(await listen<{ message: string }>('batch:log', (e) => {
    appendLog(e.payload.message)
  }))

  unlisteners.push(await listen<ProgressPayload>('batch:progress', (e) => {
    const { processed, total, status, file_name, duration_s, elapsed_s } = e.payload
    totalFiles.value = total
    if (status === 'done') stats.done++
    else if (status === 'failed') stats.failed++
    stats.pending = total - processed - (status === 'done' || status === 'failed' ? 0 : 0)
    stats.pending = Math.max(0, total - (stats.done + stats.failed))
    if (file_name) {
      currentFileName.value = file_name
      currentFileDuration.value = duration_s
    }
    if (elapsed_s > 0) elapsedSeconds.value = Math.floor(elapsed_s)
  }))

  unlisteners.push(await listen<{ success: boolean; done: number; failed: number; elapsed_s: number }>('batch:finished', (e) => {
    isRunning.value = false
    stopElapsedTimer()
    stats.done = e.payload.done
    stats.failed = e.payload.failed
    elapsedSeconds.value = Math.floor(e.payload.elapsed_s)
    currentFileName.value = ''
    if (e.payload.done === 0 && e.payload.failed === 0) {
      statusText.value = '处理结束'
    } else if (e.payload.success) {
      statusText.value = '处理完成'
      appendLog('\n[完成] 所有任务已处理完毕。')
    } else {
      statusText.value = '处理结束（有失败）'
      appendLog(`\n[结束] done=${e.payload.done}  failed=${e.payload.failed}`)
    }
  }))
}

// ── Init ───────────────────────────────────────────────────────────────────
onMounted(async () => {
  await setupListeners()
  await loadConfig()
  await detectNvenc()
})

onUnmounted(() => {
  stopElapsedTimer()
  if (animationFrame) cancelAnimationFrame(animationFrame)
  for (const unlisten of unlisteners) unlisten()
})
</script>

<template>
  <div class="min-h-screen bg-background text-foreground flex flex-col">
    <!-- Header -->
    <header class="px-6 py-4 bg-white border-b border-border/60">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-lg font-semibold tracking-tight text-foreground">视频批量处理器</h1>
          <p class="text-xs text-muted-foreground mt-0.5">批量转换 · 字幕裁剪 · GPU 加速</p>
        </div>
        <div v-if="hasNvenc" class="flex items-center gap-1.5 text-xs text-success bg-success/10 px-2.5 py-1 rounded-full font-medium">
          <span class="w-1.5 h-1.5 rounded-full bg-success"></span>
          GPU 可用
        </div>
        <div v-else class="flex items-center gap-1.5 text-xs text-muted-foreground bg-muted px-2.5 py-1 rounded-full">
          <span class="w-1.5 h-1.5 rounded-full bg-muted-foreground/40"></span>
          仅 CPU
        </div>
      </div>
    </header>

    <main class="flex-1 overflow-auto px-6 py-4 space-y-3">

      <!-- Folder settings -->
      <Card class="shadow-[0_1px_3px_rgba(0,0,0,0.06)] border-border/50">
        <CardHeader class="pb-2 pt-4 px-5">
          <CardTitle class="text-xs font-medium text-muted-foreground uppercase tracking-wider">文件夹设置</CardTitle>
        </CardHeader>
        <CardContent class="space-y-2.5 px-5 pb-4">
          <div class="flex items-center gap-2">
            <Label class="w-20 shrink-0 text-xs">输入文件夹</Label>
            <Input
              :model-value="inputDir"
              placeholder="选择包含视频文件的文件夹"
              readonly
              class="flex-1 h-8 text-xs"
            />
            <Button variant="outline" size="sm" class="h-8 text-xs" @click="pickInput">浏览</Button>
          </div>

          <div class="flex items-center gap-2">
            <Label class="w-20 shrink-0 text-xs">输出文件夹</Label>
            <Input
              :model-value="outputDir"
              placeholder="选择 MP4 输出目录"
              readonly
              class="flex-1 h-8 text-xs"
            />
            <Button variant="outline" size="sm" class="h-8 text-xs" @click="pickOutput">浏览</Button>
          </div>

          <div class="flex items-center gap-2">
            <Label class="w-20 shrink-0 text-xs">文件扩展名</Label>
            <Input
              v-model="inputExt"
              placeholder=".ts,.mp4,.mkv"
              class="flex-1 h-8 text-xs"
            />
          </div>
        </CardContent>
      </Card>

      <!-- Processing options -->
      <Card class="shadow-[0_1px_3px_rgba(0,0,0,0.06)] border-border/50">
        <CardHeader class="pb-2 pt-4 px-5">
          <CardTitle class="text-xs font-medium text-muted-foreground uppercase tracking-wider">处理选项</CardTitle>
        </CardHeader>
        <CardContent class="px-5 pb-4">
          <div class="flex items-center gap-6">
            <div class="flex items-center gap-2">
              <Label class="text-xs">并行数量</Label>
              <Input
                v-model.number="workers"
                type="number" :min="1" :max="32"
                class="w-20 h-8 text-xs"
              />
            </div>

            <div class="flex items-center gap-2">
              <Label class="text-xs">画质选择</Label>
              <Select v-model="encodeMode">
                <SelectTrigger class="w-[240px] h-8 text-xs">
                  <SelectValue placeholder="选择画质" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="quality">高画质（CPU，最佳质量）</SelectItem>
                  <SelectItem value="gpu_quality" :disabled="!hasNvenc">
                    中画质（GPU，高质量）{{ !hasNvenc ? ' — 无 GPU' : '' }}
                  </SelectItem>
                  <SelectItem value="fast" :disabled="!hasNvenc">
                    低画质（GPU，极速）{{ !hasNvenc ? ' — 无 GPU' : '' }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Shard config (compact) -->
      <Card class="shadow-[0_1px_3px_rgba(0,0,0,0.06)] border-border/50">
        <CardContent class="px-5 py-3">
          <div class="flex items-center gap-2">
            <Checkbox
              id="shard-check"
              :checked="shardEnabled"
              @update:checked="(v: boolean) => shardEnabled = v"
            />
            <Label for="shard-check" class="text-xs font-medium cursor-pointer">多机分片（可选）</Label>

            <template v-if="shardEnabled">
              <div class="flex items-center gap-2 ml-4">
                <Label class="text-xs text-muted-foreground">编号</Label>
                <Input
                  v-model.number="shardIndex"
                  type="number" :min="0" :max="99"
                  class="w-16 h-7 text-xs"
                />
              </div>
              <div class="flex items-center gap-2">
                <Label class="text-xs text-muted-foreground">总数</Label>
                <Input
                  v-model.number="shardCount"
                  type="number" :min="2" :max="100"
                  class="w-16 h-7 text-xs"
                />
              </div>
            </template>
          </div>
        </CardContent>
      </Card>

      <!-- Progress -->
      <Card class="shadow-[0_1px_3px_rgba(0,0,0,0.06)] border-border/50">
        <CardHeader class="pb-2 pt-4 px-5">
          <CardTitle class="text-xs font-medium text-muted-foreground uppercase tracking-wider">进度</CardTitle>
        </CardHeader>
        <CardContent class="px-5 pb-4 space-y-2.5">
          <!-- Top line: percentage + count + ETA -->
          <div class="flex items-baseline justify-between text-xs">
            <div class="flex items-baseline gap-2">
              <span class="text-lg font-bold tabular-nums" :class="isRunning ? 'text-primary' : 'text-foreground'">
                {{ displayPct }}%
              </span>
              <span v-if="totalFiles" class="text-muted-foreground">
                {{ stats.done + stats.failed }} / {{ totalFiles }} 已完成
              </span>
            </div>
            <div v-if="isRunning && eta" class="text-muted-foreground tabular-nums">
              ETA {{ eta }}
            </div>
          </div>

          <!-- Progress bar -->
          <div class="relative h-2.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              class="h-full rounded-full transition-all duration-500 ease-out"
              :class="stats.failed > 0 ? 'bg-gradient-to-r from-primary to-warning' : 'bg-gradient-to-r from-primary to-primary/80'"
              :style="{ width: `${animatedPct}%` }"
            />
          </div>

          <!-- Current file -->
          <div v-if="isRunning && currentFileName" class="flex items-center gap-2 text-xs text-muted-foreground">
            <span class="inline-block w-1 h-1 rounded-full bg-primary animate-pulse"></span>
            <span class="truncate">{{ currentFileName }}</span>
            <span v-if="currentFileDuration > 0" class="tabular-nums shrink-0">{{ currentFileDuration.toFixed(1) }}s</span>
          </div>

          <!-- Stats badges -->
          <div class="flex items-center gap-3 text-xs">
            <div v-if="isRunning" class="flex items-center gap-1.5 text-muted-foreground">
              <span class="tabular-nums">{{ elapsedDisplay }}</span>
              已用
            </div>
            <div class="flex items-center gap-1 text-success">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
              <span class="tabular-nums font-medium">{{ stats.done }}</span>
              <span class="text-muted-foreground">完成</span>
            </div>
            <div class="flex items-center gap-1" :class="stats.failed > 0 ? 'text-destructive' : 'text-muted-foreground'">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"/></svg>
              <span class="tabular-nums font-medium">{{ stats.failed }}</span>
              <span class="text-muted-foreground">失败</span>
            </div>
            <div class="flex items-center gap-1 text-muted-foreground">
              <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-width="2"/><path stroke-linecap="round" stroke-width="2" d="M12 6v6l4 2"/></svg>
              <span class="tabular-nums">{{ stats.pending }}</span>
              等待
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Log -->
      <Card class="shadow-[0_1px_3px_rgba(0,0,0,0.06)] border-border/50">
        <CardHeader class="pb-2 pt-4 px-5">
          <CardTitle class="text-xs font-medium text-muted-foreground uppercase tracking-wider">日志</CardTitle>
        </CardHeader>
        <CardContent class="px-5 pb-4">
          <pre
            ref="logEl"
            class="h-44 overflow-y-auto rounded-lg bg-slate-50 border border-border/30 p-3 text-[11px] font-mono leading-relaxed whitespace-pre-wrap break-all text-slate-600"
          >{{ logLines.join('\n') }}</pre>
        </CardContent>
      </Card>

    </main>

    <!-- Bottom action bar -->
    <footer class="border-t border-border/60 bg-white px-6 py-3 flex items-center gap-3">
      <Button @click="onStart" :disabled="isRunning" size="sm" class="px-5">
        <svg v-if="!isRunning" class="w-3.5 h-3.5 mr-1" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        <svg v-else class="w-3.5 h-3.5 mr-1 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-width="2" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
        {{ isRunning ? '处理中...' : '开始处理' }}
      </Button>

      <Button variant="outline" size="sm" @click="onStop" :disabled="!isRunning">
        <svg class="w-3.5 h-3.5 mr-1" fill="currentColor" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>
        停止
      </Button>

      <Button variant="outline" size="sm" @click="onStatus" :disabled="isRunning">
        查询状态
      </Button>

      <span class="ml-auto text-xs text-muted-foreground">{{ statusText }}</span>
    </footer>
  </div>
</template>
