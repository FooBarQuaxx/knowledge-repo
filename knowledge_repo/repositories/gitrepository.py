import os
import shutil
import logging
import re
import git
import socket

from knowledge_repo._version import __git_uri__
from ..repository import KnowledgeRepository
from ..utils.exec_code import get_module_for_source

logger = logging.getLogger(__name__)


class GitKnowledgeRepository(KnowledgeRepository):
    _registry_keys = ['', 'git']

    def init(self, config='git:////.knowledge_repo_config.py', auto_create=False):
        self.auto_create = auto_create
        self.path = self.uri.replace('git://', '')

        if config.startswith('git:////'):
            self.config.update(get_module_for_source(self.git_read(config.replace('git:////', '')), 'git_config'))
        else:
            self.config.update(os.path.join(self.path, config))

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        assert isinstance(path, str), "The path specified must be a string."
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            path = os.path.abspath(path)
            if self.auto_create:
                self.__repo_init(path)
            else:
                raise ValueError("Provided path '{}' does not exist.".format(path))
        assert self.__is_valid_repo(
            path), "Provided path '{}' is not a valid repository.".format(path)
        self._path = path
        self.uri = path  # Update uri to point to absolute path of repository

    def __is_valid_repo(self, path):
        try:
            git.Repo(path)
            return True
        except git.InvalidGitRepositoryError:
            return False

    def __repo_init(self, path):
        try:
            repo = git.Repo.init(path, mkdir=True)
            sm = repo.create_submodule(
                'knowledge_repo', '.resources', url=__git_uri__, branch='stable')
            shutil.copy(os.path.join(os.path.dirname(__file__), 'config_defaults.py'),
                        os.path.join(path, '.knowledge_repo_config.py'))
            shutil.copy(os.path.join(os.path.dirname(__file__), 'templates',
                                     'repo_data_readme.md'), os.path.join(path, 'README.md'))
            repo.index.add(['.knowledge_repo_config.py', 'README.md'])
            repo.index.commit("Initial commit.")
            sm.update()
        except Exception as e:
            shutil.rmtree(path)
            raise e

    @property
    def git(self):
        if not hasattr(self, '_git'):
            self._git = git.Repo(self.path)
        return self._git

    # ----------- Repository actions / state ------------------------------------
    @property
    def revision(self):
        return self.git.commit().hexsha

    def update(self, branch='master'):
        if len(self.git.remotes) == 0:
            return
        if not self.__remote_available:
            logger.warning("Cannot connect to remote repository hosted on {}. Continuing locally with potentially outdated code.".format(
                self.__remote_host))
            return
        logger.info("Fetching updates to the knowledge repository...")
        self.git.remote().fetch()
        current_branch = self.git.active_branch
        self.git.branches.master.checkout()
        self.git.remote().pull('master')
        self.git.submodules[0].update(init=True)  # TODO: FIXME
        current_branch.checkout()

    def set_active_draft(self, path):
        self.git_branch_for_post(path).checkout()

    @property
    def status(self):
        return {
            'branch': self.git.active_branch.name,
            'changed_files': [str(diff.a_path) for diff in self.git_diff()]
        }

    @property
    def status_message(self):
        status = self.status
        message = "Currently checked out on the `{branch}` branch.".format(branch=status['branch'])
        if len(status['changed_files']) > 0:
            message += "Files modified: \n {modified}".format(modified='\n\t- '.join(status['changed_files']))
        return message

    # ---------------- Git properties and actions -------------------------
    def git_dir(self, prefix=None, commit='master'):
        commit = self.git.commit(commit)
        tree = commit.tree
        if prefix is not None:
            tree = tree[prefix]
        return [o.path for o in tree.traverse(
                prune=lambda i, d: isinstance(i, git.Submodule) or os.path.dirname(i.path).endswith('.kp'),
                visit_once=False,
                predicate=lambda i, d: i.path.endswith('.kp')
                )
                ]

    def git_read(self, path, commit='master'):
        commit = self.git.commit(commit)
        return commit.tree[path].data_stream.read()

    @property
    def git_local_branches(self):
        unmerged_branches = [branch.replace(
            '*', '').strip() for branch in self.git.git.branch('--no-merged', 'master').split('\n')]
        return unmerged_branches

    def __get_path_from_ref(self, ref):
        refs = ref.split('/')
        for i, ref in enumerate(refs):
            if ref.endswith('.kp'):
                break
        if not ref.endswith('.kp'):
            return None
        return '/'.join(refs[:i + 1])

    def git_local_posts(self, branches=None, as_dict=False):
        if branches is None:
            branches = self.git_local_branches
        posts = {}
        for branch in branches:
            posts[branch] = set([self.__get_path_from_ref(diff.a_path) for diff in self.git_diff(branch)])
            posts[branch].discard(None)

        if not as_dict:
            out_posts = set()
            for branch, ps in posts.items():
                out_posts.update(ps)
            return list(out_posts)
        return posts

    def git_branch_for_post(self, path, interactive=False):
        if path is None:
            return None
        if path in self.git_local_branches:
            return self.git_branch(path)

        branches = []
        for branch in self.git_local_branches:
            if path in self.git_local_posts(branches=[branch]):
                branches.append(branch)

        if len(branches) == 0:
            if path in self.dir():
                return self.git_branch('master')
            return None

        if len(branches) == 1:
            return self.git_branch(branches[0])

        # Deal with ambiguity
        if interactive:
            print("There are multiple branches for post '{}'.".format(path))
            for i, branch in enumerate(branches):
                print("{}. {}".format(i, branch))
            response = None
            while not isinstance(response, int):
                response = raw_input('Please select the branch you would like to use: ')
                try:
                    response = int(response)
                except:
                    response = None
        else:
            response = 0

        return self.git_branch(branches[response])

    def git_branch(self, branch=None):
        if isinstance(branch, git.Head):
            return branch
        if branch is None:
            return self.git.active_branch

        if not isinstance(branch, (str, unicode)):
            raise ValueError("'{}' of type `{}` is not a valid branch descriptor.".format(branch, type(branch)))

        try:
            return self.git.branches[branch]
        except IndexError:
            raise ValueError("Specified branch `{}` does not exist.".format(branch))

    def git_checkout(self, branch, soft=False, reset=False, create=False):
        if not create:
            branch_obj = self.git_branch(branch)
            branch_obj.checkout()
            return branch_obj

        if soft and self.git.active_branch.name not in ['master', branch] and not self.git.active_branch.name.endswith('.kp'):
            response = None
            while response not in ['y', 'n']:
                response = raw_input(
                    'It looks like you have checked out the `{}` branch, whereas we were expecting to use `{}`. Do you want to use your current branch instead? (y/n) '.format(self.git.active_branch.name, branch))
                if response == 'y':
                    branch = self.git.active_branch.name

        if reset or branch not in [b.name for b in self.git.branches]:
            ref_head = None
            if len(self.git.remotes) > 0:
                for ref in self.git.remote().refs:
                    if ref.name == branch:
                        ref_head = ref
                        break
            if not ref_head:
                ref_head = self.git.remote().refs.master if len(
                    self.git.remotes) > 0 else self.git.branches.master
            else:
                logger.warning(
                    "The branch `{}` already exists as upstream, and you maybe clobbering someone's work. Please check.".format(ref_head.name))
            branch = self.git.create_head(branch, ref_head, force=True)
        else:
            branch = self.git_branch(branch)
        branch.checkout()
        return branch

    def git_diff(self, ref=None, ref_base=None, patch=False):
        commit = self.git.commit(ref)
        ref = self.git.merge_base(self.git.commit(ref_base or 'master'), commit)[0]
        return commit.diff(ref, create_patch=patch)

    # ---------------- Post retrieval methods --------------------------------

    def _dir(self, prefix, statuses):
        posts = set()

        if any([status != self.PostStatus.PUBLISHED for status in statuses]):
            local_posts = self.git_local_posts(as_dict=True)

        for status in statuses:
            if status == self.PostStatus.PUBLISHED:
                posts.update(self.git_dir(prefix=prefix, commit='master'))
            else:
                for branch in local_posts:
                    for prefix in local_posts[branch]:
                        if prefix is not None and not prefix.startswith(prefix):
                            continue
                        if self._kp_status(prefix, branch=branch) in statuses:
                            posts.add(prefix)
        for post in sorted(posts):
            yield post

    # ------------- Post submission / addition user flow ----------------------
    def _add_prepare(self, kp, path, update=False, branch=None, squash=False, message=None):
        target = os.path.abspath(os.path.join(self.path, path))
        branch = branch or path

        # Create or checkout the appropriate branch for this project
        logger.info("Checking out (and/or creating) a new branch `{}`...".format(branch))
        branch_obj = self.git_checkout(branch, soft=True, reset=squash, create=True)
        branch = branch_obj.name

        # Verify that post path does not exist (unless we are updating the post)
        assert update or not os.path.exists(target), "A knowledge post already exists at '{}'! If you wanted to update it, please pass the '--update' flag.".format(path)

        # Add knowledge post to local branch
        logger.info("Adding and committing '{}' to local branch `{}`...".format(path, branch))

    def _add_cleanup(self, kp, path, update=False, branch=None, squash=False, message=None):
        self.git.index.add([path])

        # Commit the knowledge post and rollback if it fails
        try:
            if message is None:
                message = raw_input("Please enter a commit message for this post: ")
            self.git.index.commit(message)
        except (KeyboardInterrupt, Exception) as e:
            if message is None:
                logger.warning("No commit message input for post '{}'. Rolling back post addition...")
            else:
                logger.error("Something went wrong. Rolling back post addition...")
            self.git.index.reset()
            try:
                self.git.git.clean('-df', path)
                self.git.git.checkout('--', path)
            except:
                pass
            raise e

    def _submit(self, path=None, branch=None, force=False):
        if len(self.git.remotes) == 0:
            raise RuntimeError("No remote repositories exist into which this branch can be submitted.")
        if branch is None and path is None:
            raise ValueError("To submit a knowledge post, a path to the post and/or a git branch must be specified.")
        if branch is None:
            branch = self.git_branch_for_post(path)
        if branch is None:
            raise ValueError("It does not appear that you have any drafts in progress for '{}'.".format(path))

        if not self.__remote_available:
            raise RuntimeError("Cannot connect to remote repository {}. Please check your connection, and then try again.".format(self.__remote_uri))

        self.git.remote().push(branch, force=force)
        logger.info("Pushed local branch `{}` to upstream branch `{}`. Please consider starting a pull request, or otherwise merging into master.".format(branch, branch))

    def _publish(self, path):  # Publish a post for general perusal
        raise NotImplementedError

    def _unpublish(self, path):  # unpublish a post for general perusal
        raise NotImplementedError

    def _accept(self, path):  # Approve to publish a post for general perusal
        pass

    def _remove(self, path, all=False):
        raise NotImplementedError

    # ------------ Knowledge Post Data Retrieval Methods -------------------------

    def _kp_path(self, path, rel=None):
        return KnowledgeRepository._kp_path(self, os.path.expanduser(path), rel=rel or self.path)

    def _kp_exists(self, path, revision=None):
        # For speed, first check whether it exists in the checked out branch, then search more deeply
        return os.path.isdir(os.path.join(self.path, path)) or (self.git_branch_for_post(path, interactive=False) is not None)

    def _kp_status(self, path, revision=None, detailed=False, branch=None):
        if not hasattr(self, '_dir_cache'):
            self._dir_cache = {path: None for path in self.dir()}
        if path in self._dir_cache:
            return self.PostStatus().PUBLISHED
        if branch is None:
            branch = self.git_branch_for_post(path, interactive=False)
        else:
            branch = self.git_branch(branch)

        if branch is None:
            return ValueError("No such post: {}".format(path))

        if branch.name == 'master':
            status = self.PostStatus.PUBLISHED, None
        elif branch.name in self.git.remote().refs:
            remote_branch = self.git.remote().refs[branch.name].name
            behind = len(list(self.git.iter_commits('{}..{}'.format(branch, remote_branch))))
            ahead = len(list(self.git.iter_commits('{}..{}'.format(remote_branch, branch))))

            status = (self.PostStatus.SUBMITTED,
                      (" - {} commits behind".format(behind) if behind else '') +
                      (" - {} commits ahead".format(ahead) if ahead else '') +
                      (" [On branch: {}]".format(branch) if branch != path else ''))
        else:
            status = self.PostStatus.DRAFT, None

        if detailed:
            return status
        return status[0]

    def _kp_get_revision(self, path):
        # We use a 'REVISION' file in the knowledge post folder rather than using git
        # revisions because using git rev-parse is slow.
        try:
            return int(self._kp_read_ref(path, 'REVISION'))
        except IOError:
            return 0

    def _kp_get_revisions(self, path):  # slow
        # TODO: In the future, we may want to use something like:
        #    self.git.iter_commits(paths=os.path.join(self.path, path, 'knowledge.md'))
        # But this will require a lot of piping and may not make sense in the context
        # of a non-bare git repository.
        raise NotImplementedError

    def _kp_write_ref(self, path, reference, data, revision=None):
        ref_path = os.path.join(self.path, path, reference)
        ref_dir = os.path.dirname(ref_path)
        if not os.path.exists(ref_dir):
            os.makedirs(ref_dir)
        with open(ref_path, 'w') as f:
            return f.write(data)

    def _kp_dir(self, path, parent=None, revision=None):  # TODO: Account for revision
        for dirpath, dirnames, filenames in os.walk(os.path.join(self.path, path)):
            for filename in filenames:
                if dirpath == "" and filename == "REVISION":
                    continue
                yield os.path.relpath(os.path.join(dirpath, filename), os.path.join(self.path, path))

    def _kp_has_ref(self, path, reference, revision=None):  # TODO: Account for revision
        return os.path.isfile(os.path.join(self.path, path, reference))

    def _kp_diff(self, path, head, base):
        raise NotImplementedError

    def _kp_new_revision(self, path):
        self._kp_write_ref(path, "REVISION", str(self._kp_get_revision(path) + 1))

    def _kp_read_ref(self, path, reference, revision=None):
        with open(os.path.join(self.path, path, reference)) as f:
            return f.read()

    # ------------- Utility methods --------------------------------------
    def __abspath(self, path):
        return os.path.abspath(os.path.join(self.path, path))

    @property
    def __remote_uri(self):
        try:
            return self.git.git.remote('get-url', self.git.remote().name)
        except git.exc.GitCommandError:
            return None

    @property
    def __remote_host(self):
        if self.__remote_uri:
            # TODO: support more types of hosts
            m = re.match('.*?@(.*?):\.*?', self.__remote_uri)
            if m:  # shorthand ssh uri
                return m.group(1)
        return None

    @property
    def __remote_available(self):
        # TODO: support more types of hosts
        host = self.__remote_host

        if host:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect((socket.gethostbyname(host), 22))
                return True
            except:
                return False
            finally:
                s.close()
        return True