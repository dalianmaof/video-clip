<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
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

// Event listener cleanup handles
const unlisteners: UnlistenFn[] = []

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
      ? '[INFO] NVENC gpu detected successfully.'
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

// ── Progress bar ───────────────────────────────────────────────────────────
const progressPct = computed(() => {
  if (!totalFiles.value) return 0
  return Math.round((stats.done + stats.failed) / totalFiles.value * 100)
})

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
  isRunning.value  = true
  statusText.value = '正在处理...'

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
      query: {
        output_dir: outputDir.value,
      }
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

  unlisteners.push(await listen<{ processed: number; total: number; status: string }>('batch:progress', (e) => {
    const { processed, total, status } = e.payload
    totalFiles.value = total
    if (status === 'done') {
      stats.done++
    } else {
      stats.failed++
    }
    stats.pending = total - processed
  }))

  unlisteners.push(await listen<{ success: boolean; done: number; failed: number }>('batch:finished', (e) => {
    isRunning.value = false
    stats.done = e.payload.done
    stats.failed = e.payload.failed
    if (e.payload.success) {
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
  for (const unlisten of unlisteners) {
    unlisten()
  }
})
</script>

<template>
  <div class="min-h-screen bg-background text-foreground flex flex-col">
    <!-- Header -->
    <header class="border-b px-6 py-4">
      <h1 class="text-xl font-semibold tracking-tight">视频批量处理器</h1>
    </header>

    <main class="flex-1 overflow-auto px-6 py-4 space-y-4">

      <!-- Folder settings -->
      <Card>
        <CardHeader class="pb-3">
          <CardTitle class="text-sm font-medium text-muted-foreground uppercase tracking-wider">文件夹设置</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="flex items-center gap-2">
            <Label class="w-24 shrink-0">输入文件夹</Label>
            <Input
              :model-value="inputDir"
              placeholder="选择包含视频文件的文件夹"
              readonly
              class="flex-1"
            />
            <Button variant="outline" size="sm" @click="pickInput">浏览</Button>
          </div>

          <div class="flex items-center gap-2">
            <Label class="w-24 shrink-0">输出文件夹</Label>
            <Input
              :model-value="outputDir"
              placeholder="选择 MP4 输出目录"
              readonly
              class="flex-1"
            />
            <Button variant="outline" size="sm" @click="pickOutput">浏览</Button>
          </div>

          <div class="flex items-center gap-2">
            <Label class="w-24 shrink-0">文件扩展名</Label>
            <Input
              v-model="inputExt"
              placeholder=".ts,.mp4,.mkv"
              class="flex-1"
            />
          </div>
        </CardContent>
      </Card>

      <!-- Processing options -->
      <Card>
        <CardHeader class="pb-3">
          <CardTitle class="text-sm font-medium text-muted-foreground uppercase tracking-wider">处理选项</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="flex items-center gap-2">
            <Label class="w-24 shrink-0">并行数量</Label>
            <Input
              v-model.number="workers"
              type="number" :min="1" :max="32"
              class="w-24"
            />
          </div>

          <div class="flex items-center gap-2">
            <Label class="w-24 shrink-0">画质选择</Label>
            <Select v-model="encodeMode">
              <SelectTrigger class="w-[280px]">
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
        </CardContent>
      </Card>

      <!-- Shard config -->
      <Card>
        <CardContent class="pt-6 space-y-3">
          <div class="flex items-center gap-2">
            <Checkbox
              id="shard-check"
              :checked="shardEnabled"
              @update:checked="(v: boolean) => shardEnabled = v"
            />
            <Label for="shard-check" class="font-medium cursor-pointer">多机分片（可选）</Label>
          </div>

          <div v-if="shardEnabled" class="flex flex-wrap gap-4 pl-6">
            <div class="flex items-center gap-2">
              <Label>本机编号（0 起）</Label>
              <Input
                v-model.number="shardIndex"
                type="number" :min="0" :max="99"
                class="w-20"
              />
            </div>
            <div class="flex items-center gap-2">
              <Label>总机器数量</Label>
              <Input
                v-model.number="shardCount"
                type="number" :min="2" :max="100"
                class="w-20"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- Progress -->
      <Card>
        <CardHeader class="pb-3">
          <CardTitle class="text-sm font-medium text-muted-foreground uppercase tracking-wider">进度</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <Progress :model-value="progressPct" />
          <div class="flex gap-6 text-sm">
            <span class="text-muted-foreground">等待: <strong>{{ stats.pending }}</strong></span>
            <span class="text-green-600">完成: <strong>{{ stats.done }}</strong></span>
            <span :class="stats.failed > 0 ? 'text-destructive font-bold' : 'text-muted-foreground'">
              失败: <strong>{{ stats.failed }}</strong>
            </span>
          </div>
        </CardContent>
      </Card>

      <!-- Log -->
      <Card>
        <CardHeader class="pb-3">
          <CardTitle class="text-sm font-medium text-muted-foreground uppercase tracking-wider">日志</CardTitle>
        </CardHeader>
        <CardContent>
          <pre
            ref="logEl"
            class="h-52 overflow-y-auto rounded bg-muted p-3 text-xs font-mono leading-relaxed whitespace-pre-wrap break-all"
          >{{ logLines.join('\n') }}</pre>
        </CardContent>
      </Card>

    </main>

    <!-- Bottom action bar -->
    <footer class="border-t px-6 py-3 flex items-center gap-3">
      <Button @click="onStart" :disabled="isRunning">
        开始处理
      </Button>

      <Button variant="outline" @click="onStop" :disabled="!isRunning">
        停止
      </Button>

      <Button variant="outline" @click="onStatus" :disabled="isRunning">
        查询状态
      </Button>

      <span class="ml-auto text-sm text-muted-foreground">{{ statusText }}</span>
    </footer>
  </div>
</template>
