<script setup lang="ts">
const API_BASE = "http://localhost:8000"

const models = ref<string[]>([])
const selectedModel = ref("")
const workspaces = ref<string[]>([])
const selectedWorkspace = ref("")
const prompt = ref("")
const answer = ref("")
const loading = ref(false)
const errorMessage = ref("")

// confirm待ちの状態
const pending = ref<{
  pending_id: string
  tool_name: string
  tool_args: Record<string, any>
  preview: string
} | null>(null)

const loadModels = async () => {
  try {
    const res = await $fetch<{ models: string[] }>(`${API_BASE}/api/models`)
    models.value = res.models
    if (models.value.length > 0) {
      selectedModel.value = models.value[0]
    }
  } catch (e: any) {
    errorMessage.value = `モデル一覧の取得に失敗しました: ${e.message ?? e}`
  }
}

const loadWorkspaces = async () => {
  try {
    const res = await $fetch<{ workspaces: string[] }>(`${API_BASE}/api/workspaces`)
    workspaces.value = res.workspaces
    if (workspaces.value.length > 0) {
      selectedWorkspace.value = workspaces.value[0]
    }
  } catch (e: any) {
    errorMessage.value = `ワークスペース一覧の取得に失敗しました: ${e.message ?? e}`
  }
}

onMounted(() => {
  loadModels()
  loadWorkspaces()
})

const handleChatResponse = async (res: any) => {
  if (res.status === "pending_confirmation") {
    pending.value = {
      pending_id: res.pending_id,
      tool_name: res.tool_name,
      tool_args: res.tool_args,
      preview: res.preview,
    }
  } else {
    answer.value = res.answer ?? ""
    pending.value = null
  }
}

const submitPrompt = async () => {
  if (!selectedModel.value || !selectedWorkspace.value || !prompt.value.trim()) return
  loading.value = true
  errorMessage.value = ""
  answer.value = ""
  pending.value = null
  try {
    const res = await $fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      body: { model: selectedModel.value, workspace: selectedWorkspace.value, prompt: prompt.value },
    })
    await handleChatResponse(res)
  } catch (e: any) {
    errorMessage.value = `リクエストに失敗しました: ${e.message ?? e}`
  } finally {
    loading.value = false
  }
}

const respondToConfirm = async (approved: boolean) => {
  if (!pending.value) return
  loading.value = true
  errorMessage.value = ""
  try {
    const res = await $fetch(`${API_BASE}/api/confirm`, {
      method: "POST",
      body: { pending_id: pending.value.pending_id, approved },
    })
    await handleChatResponse(res)
  } catch (e: any) {
    errorMessage.value = `確認応答の送信に失敗しました: ${e.message ?? e}`
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="container">
    <h1>Local LLM Agent</h1>

    <section class="field">
      <label for="model">モデル</label>
      <select id="model" v-model="selectedModel">
        <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
      </select>
    </section>

    <section class="field">
      <label for="workspace">ワークスペース</label>
      <select id="workspace" v-model="selectedWorkspace">
        <option v-for="w in workspaces" :key="w" :value="w">{{ w }}</option>
      </select>
    </section>

    <section class="field">
      <label for="prompt">プロンプト</label>
      <textarea
        id="prompt"
        v-model="prompt"
        rows="4"
        placeholder="例: 現在のディレクトリのファイル一覧をlsコマンドで確認して"
      />
    </section>

    <button :disabled="loading || !!pending" @click="submitPrompt">
      {{ loading ? "実行中..." : "送信" }}
    </button>

    <p v-if="errorMessage" class="error">{{ errorMessage }}</p>

    <section v-if="pending" class="confirm-box">
      <h2>確認が必要です</h2>
      <p>ツール: <code>{{ pending.tool_name }}</code></p>
      <pre>{{ pending.preview }}</pre>
      <div class="confirm-actions">
        <button :disabled="loading" @click="respondToConfirm(true)">実行する</button>
        <button :disabled="loading" @click="respondToConfirm(false)">キャンセル</button>
      </div>
    </section>

    <section v-if="answer" class="answer-box">
      <h2>回答</h2>
      <pre>{{ answer }}</pre>
    </section>
  </div>
</template>

<style scoped>
.container {
  max-width: 720px;
  margin: 2rem auto;
  padding: 0 1rem;
  font-family: system-ui, sans-serif;
}
.field {
  margin-bottom: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
textarea, select {
  padding: 0.5rem;
  font-size: 1rem;
}
button {
  padding: 0.5rem 1rem;
  font-size: 1rem;
  cursor: pointer;
}
.error {
  color: #b00020;
}
.confirm-box, .answer-box {
  margin-top: 1.5rem;
  padding: 1rem;
  border: 1px solid #ccc;
  border-radius: 4px;
}
.confirm-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
pre {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>