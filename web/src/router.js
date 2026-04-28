import { createRouter, createWebHistory } from 'vue-router'
import Analytics from './pages/Analytics.vue'
import DeviceGrid from './pages/DeviceGrid.vue'
import DeviceWork from './pages/DeviceWork.vue'
import Queue from './pages/Queue.vue'

const routes = [
  { path: '/', name: 'devices', component: DeviceGrid },
  { path: '/queue', name: 'queue', component: Queue },
  { path: '/analytics', name: 'analytics', component: Analytics },
  {
    path: '/device/:serial',
    name: 'device-work',
    component: DeviceWork,
    props: true,
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
