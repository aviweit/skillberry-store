// Copyright 2025 IBM Corp.
// Licensed under the Apache License, Version 2.0

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  PageSection,
  Title,
  Card,
  CardTitle,
  CardBody,
  Gallery,
  GalleryItem,
  Text,
  TextContent,
  Button,
} from '@patternfly/react-core';
import {
  CubeIcon,
  CodeIcon,
  FileCodeIcon,
  ServerIcon,
  ArrowRightIcon,
  DownloadIcon,
} from '@patternfly/react-icons';
import { CliDownloadModal } from '@/components/CliDownloadModal';

export function HomePage() {
  const navigate = useNavigate();
  const [isCliModalOpen, setIsCliModalOpen] = useState(false);

  interface FeatureCard {
    title: string;
    description: string;
    icon: React.ReactNode;
    color: string;
    /** Navigating cards. Mutually exclusive with `action`. */
    path?: string;
    /** Cards that do something in place, like opening the CLI download modal. */
    action?: () => void;
    actionLabel?: string;
  }

  const features: FeatureCard[] = [
    {
      title: 'Skills',
      description: 'Organize collections of tools and snippets into reusable skills.',
      icon: <CodeIcon size="xl" />,
      path: '/skills',
      color: '#3E8635',
    },
    {
      title: 'Tools',
      description: 'Manage executable tools with parameters, execution, and search capabilities.',
      icon: <CubeIcon size="xl" />,
      path: '/tools',
      color: '#0066CC',
    },
    {
      title: 'Snippets',
      description: 'Store and manage code snippets for quick reference and reuse.',
      icon: <FileCodeIcon size="xl" />,
      path: '/snippets',
      color: '#A18FFF',
    },
    {
      title: 'Virtual MCP Servers',
      description: 'Create and manage virtual MCP servers for tool subsets.',
      icon: <ServerIcon size="xl" />,
      path: '/vmcp-servers',
      color: '#F0AB00',
    },
    {
      title: 'CLI',
      description:
        'Download the sbs command-line interface — a single native binary, already pointed at this store.',
      icon: <DownloadIcon />,
      // No path: this card opens the download modal rather than navigating, so
      // the card's click handler branches on `action` below.
      action: () => setIsCliModalOpen(true),
      actionLabel: 'Download CLI',
      color: '#6A6E73',
    },
  ];

  return (
    <>
      <PageSection variant="light">
        <TextContent>
          <Title headingLevel="h1" size="2xl">
            Welcome to Skillberry Store
          </Title>
          <Text>
            A smart skills repository for agentic workflows. Manage, execute, and organize
            your skills, tools and snippets with powerful search and lifecycle management.
          </Text>
        </TextContent>
      </PageSection>

      <PageSection>
        <Gallery hasGutter minWidths={{ default: '100%', md: '50%', xl: '25%' }}>
          {features.map((feature) => (
            <GalleryItem key={feature.title}>
              <Card
                isFullHeight
                isClickable
                onClick={() =>
                  feature.action ? feature.action() : navigate(feature.path!)
                }
              >
                <CardTitle>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <span style={{ color: feature.color }}>{feature.icon}</span>
                    <span>{feature.title}</span>
                  </div>
                </CardTitle>
                <CardBody>
                  <Text>{feature.description}</Text>
                  <div style={{ marginTop: '1rem' }}>
                    <Button
                      variant="link"
                      isInline
                      icon={<ArrowRightIcon />}
                      iconPosition="right"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (feature.action) {
                          feature.action();
                        } else {
                          navigate(feature.path!);
                        }
                      }}
                    >
                      {feature.actionLabel ?? `View ${feature.title}`}
                    </Button>
                  </div>
                </CardBody>
              </Card>
            </GalleryItem>
          ))}
        </Gallery>
      </PageSection>

      <CliDownloadModal
        isOpen={isCliModalOpen}
        onClose={() => setIsCliModalOpen(false)}
      />

      <PageSection variant="light">
        <TextContent>
          <Title headingLevel="h2" size="xl">
            Key Features
          </Title>
          <ul>
            <li>Add, update, delete, and execute tools (with sandboxing)</li>
            <li>Semantic and classic search across all resources</li>
            <li>Tools lifecycle management (state, visibility)</li>
            <li>Persistence to filesystem and GitHub repositories</li>
            <li>OpenAPI and MCP frontends for integration</li>
            <li>Support for multiple MCP backend servers</li>
            <li>Observability with metrics and traces</li>
          </ul>
        </TextContent>
      </PageSection>
    </>
  );
}