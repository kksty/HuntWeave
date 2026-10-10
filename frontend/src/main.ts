import { createApp } from 'vue';
import { createPinia } from 'pinia';
import { createRouter, createWebHistory } from 'vue-router';
import App from './App.vue';
// 顺序：design token → 自托管 @font-face 声明 → 控制台样式表。
import './tokens.css';
import './fonts.css';
import './style.css';

const router = createRouter({ history: createWebHistory(), routes: [{ path: '/', component: App }, { path: '/runs/:id', component: App }] });
createApp(App).use(createPinia()).use(router).mount('#app');
