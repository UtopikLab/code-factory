import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './styles.css'
import Management from './Management.tsx'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5000 } } })

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}><Management /></QueryClientProvider>
  </StrictMode>,
)
