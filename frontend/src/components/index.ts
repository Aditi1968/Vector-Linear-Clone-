/**
 * The shared component layer: Vector's UI primitives.
 *
 * Everything here is presentation. Nothing in this directory imports from
 * `features/`, knows a GraphQL type, or fetches anything -- which is what makes
 * a primitive reusable rather than a component that happens to live in a shared
 * folder.
 *
 * A primitive earns its place here when it is used by more than one feature or
 * is load-bearing for accessibility (the indicators, the dialog, the toast
 * region). Anything used by exactly one feature still belongs inside that
 * feature.
 */

export { Button, IconButton } from './Button'
export type {
  ButtonProps,
  ButtonSize,
  ButtonVariant,
  IconButtonProps,
} from './Button'

export { Checkbox, Input, Select, Textarea } from './Field'
export type {
  CheckboxProps,
  FieldSize,
  InputProps,
  SelectProps,
  TextareaProps,
} from './Field'

export { Menu } from './Menu'
export type { MenuItem, MenuProps } from './Menu'

export { Dialog } from './Dialog'
export type { DialogProps, DialogSize } from './Dialog'

export { Tooltip } from './Tooltip'
export type { TooltipProps } from './Tooltip'

export { Tabs } from './Tabs'
export type { TabItem, TabsProps } from './Tabs'

export { SegmentedControl } from './SegmentedControl'
export type { SegmentedControlProps, SegmentedOption } from './SegmentedControl'

export { ToastProvider, useToast } from './Toast'
export type { ToastOptions, ToastProviderProps, ToastTone } from './Toast'

export { Badge, Tag } from './Badge'
export type { BadgeProps, BadgeTone, TagProps } from './Badge'

export { Avatar } from './Avatar'
export type { AvatarProps, AvatarSize } from './Avatar'

export { Kbd } from './Kbd'
export type { KbdProps } from './Kbd'

export { Spinner } from './Spinner'
export type { SpinnerProps } from './Spinner'

export { Skeleton } from './Skeleton'
export type { SkeletonProps } from './Skeleton'

export { List, ListRow, ListRowMain, ListRowMeta } from './List'
export type { ListProps, ListRowProps, ListRowSlotProps } from './List'

/* The issue list's two shapes. Presentational: neither knows a GraphQL type
 * or a route, which is what lets triage, favourites, saved views and a team's
 * issues draw the same list from four different queries. */
export { IssueRow } from './IssueRow'
export type { IssueRowProps } from './IssueRow'

export { GroupHeader } from './GroupHeader'
export type { GroupHeaderProps } from './GroupHeader'

export { InspectorPanel } from './InspectorPanel'
export type { InspectorPanelProps } from './InspectorPanel'

export { EmptyState, ErrorState, PermissionState } from './States'
export type {
  EmptyStateProps,
  ErrorStateProps,
  PermissionStateProps,
} from './States'

export { VisuallyHidden } from './VisuallyHidden'
export type { VisuallyHiddenProps } from './VisuallyHidden'

/* The meaning-bearing glyphs. See components/indicators for why they are not
 * filed with the decorative icons. */
export {
  PriorityIndicator,
  ProgressIndicator,
  RelationIndicator,
  relationKindFrom,
  StatusIndicator,
  statusCategoryFrom,
} from './indicators'
export type {
  PriorityIndicatorProps,
  PriorityLevel,
  ProgressIndicatorProps,
  RelationIndicatorProps,
  RelationKind,
  StatusCategory,
  StatusIndicatorProps,
} from './indicators'

export {
  AlertIcon,
  BlockedIcon,
  BoardIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  CloseIcon,
  CommentIcon,
  CycleIcon,
  InboxIcon,
  IssueIcon,
  IssuesIcon,
  LabelIcon,
  MinusIcon,
  MoreIcon,
  PlusIcon,
  ProjectIcon,
  RelationIcon,
  SearchIcon,
  SettingsIcon,
  SubIssueIcon,
  TeamIcon,
  VectorMark,
} from './icons'
export type { IconProps } from './icons'

export { cx } from './cx'
export type { ClassValue } from './cx'
