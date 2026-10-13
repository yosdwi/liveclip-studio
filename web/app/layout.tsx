import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = { title: 'LiveClip Studio', description:'Record, rewind, clip, and export live broadcasts' };
export default function RootLayout({ children }: Readonly<{children:React.ReactNode}>) {
  return <html lang="en"><body>{children}</body></html>;
}
