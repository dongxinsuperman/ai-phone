// Explicit Server contract fixtures. Never infer these facts from raw VM state or cleanup fields.
const NONE = { start: false, stop: false, dispatch: false, delete: false, edit: false, copy: false }

export const LIFE = {
  draft: {
    state: 'draft', agent_online: false,
    actions: { ...NONE, dispatch: true, delete: true, edit: true, copy: true },
    notice: '', error: '',
  },
  stopped: {
    state: 'stopped', agent_online: true,
    actions: { ...NONE, start: true, dispatch: true, delete: true, edit: true, copy: true },
    notice: '', error: '',
  },
  running: {
    state: 'running', agent_online: true,
    actions: { ...NONE, stop: true, copy: true },
    notice: '', error: '',
  },
  starting: {
    state: 'starting', agent_online: true, actions: { ...NONE },
    notice: '本次启动正在进行。', error: '',
  },
  stopping: {
    state: 'stopping', agent_online: true, actions: { ...NONE },
    notice: '本次停止正在进行。', error: '',
  },
  unknownOnline: {
    state: null, agent_online: true,
    actions: { ...NONE, start: true, stop: true, dispatch: true, delete: true, edit: true, copy: true },
    notice: '实例状态未核实，普通操作会先核验现场。', error: '',
  },
  unknownOffline: {
    state: null, agent_online: false,
    actions: { ...NONE, dispatch: true, delete: true, edit: true, copy: true },
    notice: '所属 Agent 已离线，当前不能启停。', error: '',
  },
}
