import { createRouter, createWebHistory } from 'vue-router'
import Analytics from './pages/Analytics.vue'
import AppDistribution from './pages/AppDistribution.vue'
import DeviceConfig from './pages/DeviceConfig.vue'
import DeviceGrid from './pages/DeviceGrid.vue'
import DeviceWork from './pages/DeviceWork.vue'
import Queue from './pages/Queue.vue'
import VirtualMachines from './pages/VirtualMachines.vue'

const routes = [
  { path: '/', name: 'devices', component: DeviceGrid },
  { path: '/queue', name: 'queue', component: Queue },
  { path: '/app-distribution', name: 'app-distribution', component: AppDistribution },
  { path: '/virtual-machines', name: 'virtual-machines', component: VirtualMachines },
  { path: '/device-config', name: 'device-config', component: DeviceConfig },
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
