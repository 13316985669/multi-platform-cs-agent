import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import AntdProvider from './components/AntdProvider'
import './index.css'

const basename = import.meta.env.BASE_URL.replace(/\/$/, '')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AntdProvider>
      <BrowserRouter basename={basename || undefined}>
        <App />
      </BrowserRouter>
    </AntdProvider>
  </React.StrictMode>,
)
