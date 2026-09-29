import { Link, type LinkProps } from 'react-router'

import { useRunTo } from '@/lib/run'

/** Link that keeps the pinned run (`?run=`). */
export function RunLink({ to, ...props }: LinkProps) {
  const runTo = useRunTo()
  return <Link to={runTo(to)} {...props} />
}
