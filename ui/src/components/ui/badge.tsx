import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary/20 text-primary",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground",
        destructive:
          "border-[rgba(255,93,108,0.4)] bg-[rgba(255,93,108,0.15)] text-[#ff5d6c]",
        outline:
          "border-border text-muted-foreground",
        success:
          "border-[rgba(46,194,138,0.4)] bg-[rgba(46,194,138,0.15)] text-[#2ec28a]",
        warning:
          "border-[rgba(245,166,35,0.4)] bg-[rgba(245,166,35,0.15)] text-[#f5a623]",
        info:
          "border-[rgba(79,140,255,0.4)] bg-[rgba(79,140,255,0.15)] text-[#4f8cff]",
        teal:
          "border-[rgba(40,194,160,0.4)] bg-[rgba(40,194,160,0.15)] text-[#28c2a0]",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
