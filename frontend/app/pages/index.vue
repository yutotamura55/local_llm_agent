<script setup lang="ts">
const API_BASE = "http://localhost:8000"

const models = ref<string[]>([])
const selectedModel = ref("")
const workspaces = ref<string[]>([])
const selectedWorkspace = ref("")
const prompt = ref("")
const loading = ref(false)
const errorMessage = ref("")

// バックエンドが発行するsession_id。これを送り続けることで会話の続きとして扱われる。
// nullの間は「まだ会話を始めていない/新しい会話」を意味する。
const sessionId = ref<string | null>(null)

// 表示用の会話ログ(バックエンドの内部メッセージ配列とは別に、UI表示のためだけに保持する)
const conversation = ref<{ role: "user" | "assistant"; content: string }[]>([])

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
  sessionId.value = res.session_id ?? sessionId.value

  if (res.status === "pending_confirmation") {
    pending.value = {
      pending_id: res.pending_id,
      tool_name: res.tool_name,
      tool_args: res.tool_args,
      preview: res.preview,
    }
  } else {
    conversation.value.push({ role: "assistant", content: res.answer ?? "" })
    pending.value = null
  }
}

const submitPrompt = async () => {
  if (!selectedModel.value || !selectedWorkspace.value || !prompt.value.trim()) return
  if (loading.value || pending.value) return

  const currentPrompt = prompt.value
  conversation.value.push({ role: "user", content: currentPrompt })
  prompt.value = ""

  loading.value = true
  errorMessage.value = ""
  try {
    const res = await $fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      body: {
        model: selectedModel.value,
        workspace: selectedWorkspace.value,
        prompt: currentPrompt,
        session_id: sessionId.value,
      },
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

const startNewConversation = () => {
  sessionId.value = null
  conversation.value = []
  pending.value = null
  prompt.value = ""
  errorMessage.value = ""
}
</script>

<template>
  <div class="container">
    <div class="header">
      <h1>Local LLM Agent</h1>
      <button class="secondary" @click="startNewConversation">新しい会話</button>
    </div>

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

    <section v-if="conversation.length > 0" class="conversation">
      <div
        v-for="(turn, i) in conversation"
        :key="i"
        class="turn"
        :class="turn.role"
      >
        <div class="turn-label">{{ turn.role === "user" ? "あなた" : "エージェント" }}</div>
        <pre>{{ turn.content }}</pre>
      </div>
    </section>

    <section class="field">
      <label for="prompt">プロンプト</label>
      <textarea
        id="prompt"
        v-model="prompt"
        rows="4"
        placeholder="例: 現在のディレクトリのファイル一覧をlsコマンドで確認して(Ctrl+Enterで送信)"
        @keydown.ctrl.enter="submitPrompt"
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
  </div>
</template>

<style scoped>
.container {
  max-width: 720px;
  margin: 2rem auto;
  padding: 0 1rem;
  font-family: system-ui, sans-serif;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
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
button.secondary {
  background: transparent;
  border: 1px solid #ccc;
}
.error {
  color: #b00020;
}
.confirm-box {
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
.conversation {
  margin-bottom: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.turn {
  padding: 0.75rem;
  border-radius: 4px;
}
.turn.user {
  background: #f0f4ff;
  align-self: flex-end;
  max-width: 85%;
}
.turn.assistant {
  background: #f5f5f5;
  align-self: flex-start;
  max-width: 85%;
}
.turn-label {
  font-size: 0.75rem;
  color: #666;
  margin-bottom: 0.25rem;
}
pre {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  font-family: inherit;
}
</style>