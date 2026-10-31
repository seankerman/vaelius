"""Migration 048: projects become workspaces, conservatively (docs/ACCESS_MODEL.md section 11).

Builds the pre-048 schema with a few representative rows (project ceilings,
explicit and directory members, a raw grant, items at each stored level, a
credential holding `share`), applies 048 and checks the mapping: same ids,
members become writers through `<project>-members`, no organization or admin
entry, every existing item owner-private and every existing grant dropped.
Synthetic data only.
"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from agenthub.postgres import connect, migrate
from test_cloud_postgres import SERVICES


@unittest.skipUnless(SERVICES, 'explicit disposable PostgreSQL schema required')
class AccessMigrationTests(unittest.TestCase):
    def setUp(self):
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        services = json.loads(Path(SERVICES).read_text())
        self.item = services['tenants']['acme']
        self.schema = 'test_access_mig_' + uuid.uuid4().hex[:12]
        with connect(self.item['admin_dsn']) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.admin_dsn = make_conninfo(self.item['admin_dsn'], options='-c search_path=' + self.schema + ',public')
        self.dsn = make_conninfo(self.item['dsn'], options='-c search_path=' + self.schema + ',public')
        self.temp = tempfile.TemporaryDirectory(prefix='vaelius-migration-')

    def tearDown(self):
        from psycopg import sql
        with connect(self.item['admin_dsn']) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.temp.cleanup()

    def seed_pre_048(self):
        receipts = migrate(self.admin_dsn, through=47)
        self.assertEqual(receipts[-1]['version'], 47)
        with connect(self.admin_dsn) as db:
            run = db.execute
            run("INSERT INTO enterprise_organizations(id,max_sharing,default_max_sharing) VALUES('acme','team','team')")
            for name, level in (('alice', 'private'), ('bob', 'organization'), ('carol', 'team'), ('dave', 'team')):
                run('INSERT INTO enterprise_principals(tenant,id,max_sharing) VALUES(%s,%s,%s)', ('acme', name, level))
            run("INSERT INTO enterprise_projects(tenant,id,max_sharing) VALUES('acme','maple','organization')")
            run("INSERT INTO enterprise_projects(tenant,id,max_sharing) VALUES('acme','birch','private')")
            for project, principal in (('maple', 'alice'), ('maple', 'bob'), ('birch', 'carol')):
                run('INSERT INTO enterprise_memberships VALUES(%s,%s,%s,1)', ('acme', project, principal))
                run('INSERT INTO cloud_explicit_memberships VALUES(%s,%s)', (project, principal))
            # dave reaches maple only through a directory group mapped to it.
            run("INSERT INTO cloud_directory_users VALUES('u-dave','dave')")
            run('''INSERT INTO cloud_directory_resources VALUES('group','grp-1',1,1,%s,0)''',
                (json.dumps({'kind': 'group', 'external_id': 'grp-1', 'display': 'Platform',
                             'members': ['u-dave'], 'active': True}),))
            run("INSERT INTO cloud_directory_group_projects VALUES('grp-1','maple')")
            run("INSERT INTO cloud_directory_memberships VALUES('maple','dave')")
            run("INSERT INTO enterprise_memberships VALUES('acme','maple','dave',1)")
            for ident, owner, project, level in (('s-private', 'alice', 'maple', 'private'),
                                                 ('s-team', 'bob', 'maple', 'team'),
                                                 ('s-org', 'bob', 'maple', 'organization'),
                                                 ('s-birch', 'carol', 'birch', 'team')):
                run('''INSERT INTO enterprise_sources(id,tenant,owner,external_project,internal_project,external_id,
                    enrollment,payload_hash,visibility,occurred_at,created,sharing,sharing_requested,
                    originals_sharing) VALUES(%s,'acme',%s,%s,'enterprise:x',%s,'e',%s,'organization','0',0,%s,%s,%s)''',
                    (ident, owner, project, ident, ident, level, level, level))
            run('''INSERT INTO raw_source_grants(tenant,scope_kind,scope_id,grantee_kind,grantee,granted_by,created,
                passages,originals,issued_with) VALUES('acme','source','s-team','project','birch','bob',0,1,1,'share')''')
            run('''INSERT INTO raw_source_grants(tenant,scope_kind,scope_id,grantee_kind,grantee,granted_by,created,
                passages,originals,issued_with) VALUES('acme','source','s-org','principal','carol','bob',0,1,0,'share')''')
            run('''INSERT INTO enterprise_credentials(digest,tenant,principal,acting_for,enrollment,actions,active,created)
                VALUES('d-bob','acme','bob',NULL,'bob','["read","share"]',1,0)''')

    def test_projects_become_workspaces_privately_and_members_become_writers(self):
        self.seed_pre_048()
        migrate(self.admin_dsn)
        with connect(self.admin_dsn) as db:
            q = lambda sql, *args: db.execute(sql, args).fetchall()
            self.assertEqual(sorted((r[0], r[1], r[2]) for r in q('SELECT id,name,item_default FROM enterprise_projects')),
                             [('birch', 'birch', 'private'), ('maple', 'maple', 'shared')])
            # Members are writers through the members group (`member` since 055); nobody is an
            # admin; no organization entry.
            self.assertEqual(sorted(tuple(r.values()) for r in q('''SELECT workspace,subject_kind,subject,role
                FROM workspace_entries ORDER BY 1''')),
                [('birch', 'group', 'birch-members', 'member'), ('maple', 'group', 'maple-members', 'member')])
            self.assertEqual(q('SELECT count(*) FROM enterprise_projects WHERE org_role>0')[0][0], 0)
            access = sorted((r[0], r[1], max(r[2], r[3])) for r in q(
                'SELECT workspace,principal,principal_role,group_role FROM workspace_access'))
            self.assertEqual(access, [('birch', 'carol', 2), ('maple', 'alice', 2), ('maple', 'bob', 2),
                                      ('maple', 'dave', 2)])
            # The directory group is a scim group nested in the members group.
            nested = q('''SELECT g.source,g.external_id,e.group_id FROM groups g JOIN group_edges e
                ON e.member_kind='group' AND e.member=g.id''')
            self.assertEqual([tuple(r.values()) for r in nested], [('scim', 'grp-1', 'maple-members')])
            # Every existing item is owner-private; every existing grant is dropped.
            self.assertEqual({r[0] for r in q('SELECT private FROM enterprise_sources')}, {1})
            self.assertEqual(q('SELECT count(*) FROM raw_source_grants')[0][0], 0)
            # Settings map to their nearest equivalents.
            self.assertEqual(q("SELECT widest_entry FROM enterprise_organizations WHERE id='acme'")[0][0],
                             'people_and_groups')
            self.assertEqual(dict((r[0], r[1]) for r in q('SELECT id,contributions_default FROM enterprise_principals')),
                             {'alice': 'private', 'bob': 'shared', 'carol': 'shared', 'dave': 'shared'})
            # The read-only view keeps the old name for operator scripts.
            self.assertEqual(sorted(tuple(r.values()) for r in q('SELECT project,principal,active FROM enterprise_memberships')),
                             [('birch', 'carol', 1), ('maple', 'alice', 1), ('maple', 'bob', 1), ('maple', 'dave', 1)])
            legacy = q("SELECT actions,legacy_actions FROM enterprise_credentials WHERE digest='d-bob'")[0]
        from agenthub.enterprise import effective_actions
        self.assertIn('manage', effective_actions(legacy[0], legacy[1]))
        # Through every layer only owners read their items after the upgrade.
        self.assertEqual(self.readers(), {'s-private': ['alice'], 's-team': ['bob'], 's-org': ['bob'],
                                          's-birch': ['carol']})

    def readers(self):
        """Principals each source's policy admits through the row-level predicate."""
        with connect(self.admin_dsn) as db:
            rows = db.execute('''SELECT s.id,array_agg(who.id ORDER BY who.id) readers FROM enterprise_sources s
                JOIN search_source_policies m ON m.source_id=s.id JOIN search_policies p ON p.id=m.policy_id
                JOIN enterprise_principals who ON who.tenant=s.tenant
                WHERE search_reader_in_audience(p.definition,s.tenant,who.id,NULL,false) GROUP BY s.id''').fetchall()
        return {row[0]: row[1] for row in rows}


    def test_054_removes_github_groups_so_a_former_github_group_grants_nothing(self):
        migrate(self.admin_dsn, through=53)
        with connect(self.admin_dsn) as db:
            run = db.execute
            run("INSERT INTO enterprise_organizations(id) VALUES('acme')")
            for name in ('alice', 'erin', 'sam'):
                run("INSERT INTO enterprise_principals(tenant,id) VALUES('acme',%s)", (name,))
            for name in ('oak', 'pine'):
                run("INSERT INTO enterprise_projects(tenant,id,name) VALUES('acme',%s,%s)", (name, name))
            run("""INSERT INTO groups(tenant,id,name,source,external_id,created)
                VALUES('acme','gh-7','gh_org_team','github','7',0)""")
            run("INSERT INTO groups(tenant,id,name,source,created) VALUES('acme','crew','crew','local',0)")
            run("INSERT INTO groups(tenant,id,name,source,external_id,created) VALUES('acme','scim-a','scim-a','scim','a',0)")
            edges = (('gh-7', 'principal', 'erin'), ('crew', 'group', 'gh-7'), ('crew', 'principal', 'alice'),
                     ('scim-a', 'principal', 'sam'))
            for group, kind, member in edges:
                run("INSERT INTO group_edges(tenant,group_id,member_kind,member,created) VALUES('acme',%s,%s,%s,0)",
                    (group, kind, member))
            for workspace, kind, subject, role in (('oak', 'group', 'gh-7', 'writer'), ('pine', 'group', 'crew', 'reader'),
                                                   ('oak', 'principal', 'alice', 'admin'),
                                                   ('oak', 'group', 'scim-a', 'reader')):
                run("""INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
                    VALUES('acme',%s,%s,%s,%s,'alice',0)""", (workspace, kind, subject, role))
            run("SELECT access_refresh_groups('acme',NULL)")
            run("""INSERT INTO identity_links(tenant,provider,external_id,principal,login,linked,linked_by)
                VALUES('acme','github','41','erin','erin-gh',0,'erin')""")
            run("""INSERT INTO raw_source_grants(tenant,scope_kind,scope_id,grantee_kind,grantee,granted_by,created)
                VALUES('acme','source','s-1','group','gh-7','alice',0)""")
            roles = lambda: {(r[0], r[1]) for r in run("""SELECT principal,workspace FROM workspace_access
                WHERE tenant='acme'""").fetchall()}
            self.assertIn(('erin', 'oak'), roles(), 'before: the github group grants oak')
            self.assertIn(('erin', 'pine'), roles(), 'and pine through the local group nesting it')
            epoch = run("SELECT epoch FROM enterprise_projects WHERE id='oak'").fetchone()[0]
        migrate(self.admin_dsn)
        with connect(self.admin_dsn) as db:
            run = db.execute
            self.assertEqual({p for p, _ in roles()}, {'alice', 'sam'}, 'the former github group grants nothing')
            self.assertEqual(roles(), {('alice', 'oak'), ('alice', 'pine'), ('sam', 'oak')},
                             'local and scim groups and the admin entry are untouched')
            self.assertEqual(run("SELECT count(*) FROM groups WHERE source='github' OR id='gh-7'").fetchone()[0], 0)
            self.assertEqual(run("SELECT count(*) FROM group_closure WHERE principal='erin'").fetchone()[0], 0)
            self.assertEqual(run("SELECT count(*) FROM workspace_entries WHERE subject='gh-7'").fetchone()[0], 0)
            self.assertIsNotNone(run("SELECT revoked FROM raw_source_grants WHERE grantee='gh-7'").fetchone()[0],
                                 'a share to it is revoked, kept for audit')
            self.assertGreater(run("SELECT epoch FROM enterprise_projects WHERE id='oak'").fetchone()[0], epoch)
            for table in ('identity_links', 'identity_link_flows', 'github_installations', 'github_teams',
                          'github_team_members', 'github_webhook_deliveries'):
                self.assertIsNone(run("SELECT to_regclass(current_schema()||'.'||%s)", (table,)).fetchone()[0], table)
            with self.assertRaises(Exception, msg='the github source is gone'):
                run("INSERT INTO groups(tenant,id,name,source,created) VALUES('acme','x','x','github',0)")

    def test_055_collapses_roles_to_member_and_admin_and_the_organization_entry_only_reads(self):
        migrate(self.admin_dsn, through=54)
        with connect(self.admin_dsn) as db:
            run = db.execute
            run("INSERT INTO enterprise_organizations(id) VALUES('acme')")
            for name in ('alice', 'bob', 'carol', 'dave', 'erin'):
                run("INSERT INTO enterprise_principals(tenant,id) VALUES('acme',%s)", (name,))
            for name in ('oak', 'pine'):
                run("INSERT INTO enterprise_projects(tenant,id,name) VALUES('acme',%s,%s)", (name, name))
            run("INSERT INTO groups(tenant,id,name,source,created) VALUES('acme','crew','crew','local',0)")
            run("INSERT INTO group_edges(tenant,group_id,member_kind,member,created) VALUES('acme','crew','principal','dave',0)")
            for workspace, kind, subject, role in (('oak', 'principal', 'alice', 'admin'),
                                                   ('oak', 'principal', 'bob', 'reader'),
                                                   ('oak', 'principal', 'carol', 'writer'),
                                                   ('oak', 'group', 'crew', 'reader'),
                                                   ('pine', 'principal', 'alice', 'admin'),
                                                   ('pine', 'organization', '', 'writer')):
                run("""INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
                    VALUES('acme',%s,%s,%s,%s,'alice',0)""", (workspace, kind, subject, role))
            run("SELECT access_refresh_groups('acme',NULL)")
            epoch = run("SELECT epoch FROM enterprise_projects WHERE id='oak'").fetchone()[0]
        migrate(self.admin_dsn)
        with connect(self.admin_dsn) as db:
            run = db.execute
            entries = {(r[0], r[1], r[2]): r[3] for r in run('''SELECT workspace,subject_kind,subject,role
                FROM workspace_entries''').fetchall()}
            self.assertEqual(entries, {('oak', 'principal', 'alice'): 'admin', ('oak', 'principal', 'bob'): 'member',
                                       ('oak', 'principal', 'carol'): 'member', ('oak', 'group', 'crew'): 'member',
                                       ('pine', 'principal', 'alice'): 'admin', ('pine', 'organization', ''): 'reader'})
            ranks = {(r[0], r[1]): max(r[2], r[3]) for r in run('''SELECT workspace,principal,principal_role,group_role
                FROM workspace_access''').fetchall()}
            self.assertEqual(ranks, {('oak', 'alice'): 3, ('oak', 'bob'): 2, ('oak', 'carol'): 2, ('oak', 'dave'): 2,
                                     ('pine', 'alice'): 3})
            self.assertEqual(run("SELECT org_role FROM enterprise_projects WHERE id='pine'").fetchone()[0], 1,
                             'org visibility reads; it never writes')
            role = lambda who, where: run("SELECT (search_workspace_role('acme',%s,%s,NULL)).role", (who, where)).fetchone()[0]
            self.assertEqual((role('erin', 'pine'), role('erin', 'oak'), role('bob', 'oak')), (1, 0, 2))
            self.assertGreater(run("SELECT epoch FROM enterprise_projects WHERE id='oak'").fetchone()[0], epoch)
            for kind, subject, role_name in (('principal', 'erin', 'reader'), ('principal', 'erin', 'writer'),
                                             ('organization', '', 'member')):
                with self.assertRaises(Exception, msg=role_name):
                    with db.transaction():
                        run("""INSERT INTO workspace_entries(tenant,workspace,subject_kind,subject,role,granted_by,created)
                            VALUES('acme','oak',%s,%s,%s,'alice',0)""", (kind, subject, role_name))


if __name__ == '__main__':
    unittest.main()
