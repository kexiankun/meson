#!/usr/bin/python3 -tt

# Copyright 2012 Jussi Pakkanen

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os, stat, re, pickle
import interpreter, nodes
import environment
from meson_install import InstallData

def shell_quote(cmdlist):
    return ["'" + x + "'" for x in cmdlist]

def ninja_quote(text):
    return text.replace(' ', '$ ')

def do_conf_file(src, dst, variables):
    data = open(src).readlines()
    regex = re.compile('@(.*?)@')
    result = []
    for line in data:
        match = re.search(regex, line)
        while match:
            varname = match.group(1)
            if varname in variables:
                var = variables[varname]
                if isinstance(var, str):
                    pass
                elif isinstance(var, nodes.StringStatement):
                    var = var.get_value()
                else:
                    raise RuntimeError('Tried to replace a variable with something other than a string.')
            else:
                var = ''
            line = line.replace('@' + varname + '@', var)
            match = re.search(regex, line)
        result.append(line)
    open(dst, 'w').writelines(result)


class Backend():
    def __init__(self, build, interp):
        self.build = build
        self.environment = build.environment
        self.interpreter = interp
        self.processed_targets = {}
        self.build_to_src = os.path.relpath(self.environment.get_source_dir(),
                                            self.environment.get_build_dir())

    def get_compiler_for_source(self, src):
        for i in self.build.compilers:
            if i.can_compile(src):
                return i
        raise RuntimeError('No specified compiler can handle file ' + src)

    def get_target_filename(self, target):
        targetdir = self.get_target_dir(target)
        filename = os.path.join(targetdir, target.get_filename())
        return filename

    def get_target_dir(self, target):
        dirname = target.get_subdir()
        os.makedirs(os.path.join(self.environment.get_build_dir(), dirname), exist_ok=True)
        return dirname
    
    def get_target_private_dir(self, target):
        dirname = os.path.join(self.get_target_dir(target), target.get_basename() + '.dir')
        os.makedirs(os.path.join(self.environment.get_build_dir(), dirname), exist_ok=True)
        return dirname

    def generate_target(self, target, outfile):
        name = target.get_basename()
        if name in self.processed_targets:
            return
        self.process_target_dependencies(target, outfile)
        print('Generating target', name)
        self.generate_custom_generator_rules(target, outfile)
        outname = self.get_target_filename(target)
        obj_list = []
        if target.has_pch():
            self.generate_pch(target, outfile)
        for src in target.get_sources():
            obj_list.append(self.generate_single_compile(target, outfile, src))
        for genlist in target.get_generated_sources():
            for src in genlist.get_outfilelist():
                obj_list.append(self.generate_single_compile(target, outfile, src, True))
        self.generate_link(target, outfile, outname, obj_list)
        self.generate_shlib_aliases(target, self.get_target_dir(target), outfile)
        self.processed_targets[name] = True

    def process_target_dependencies(self, target, outfile):
        for t in target.get_dependencies():
            tname = t.get_basename()
            if not tname in self.processed_targets:
                self.generate_target(t, outfile)

    def get_pch_include_args(self, compiler, target):
        args = []
        pchpath = self.get_target_private_dir(target)
        includearg = compiler.get_include_arg(pchpath)
        for p in target.get_pch():
            if compiler.can_compile(p):
                args.append('-include')
                args.append(os.path.split(p)[-1])
        if len(args) > 0:
            args = [includearg] + args
        return args

    def generate_basic_compiler_flags(self, target, compiler):
        commands = []
        commands += self.build.get_global_flags(compiler)
        commands += target.get_extra_args(compiler.get_language())
        if self.environment.new_coredata.buildtype != 'plain':
            commands += compiler.get_debug_flags()
        if self.environment.new_coredata.buildtype == 'optimized':
            commands += compiler.get_std_opt_flags()
        if self.environment.new_coredata.coverage:
            commands += compiler.get_coverage_flags()
        commands += compiler.get_std_warn_flags()
        if isinstance(target, interpreter.SharedLibrary):
            commands += compiler.get_pic_flags()
        for dep in target.get_external_deps():
            commands += dep.get_compile_flags()
        return commands

    def build_target_link_arguments(self, deps):
        args = []
        for d in deps:
            if not isinstance(d, interpreter.StaticLibrary) and\
            not isinstance(d, interpreter.SharedLibrary):
                raise RuntimeError('Tried to link with a non-library target "%s".' % d.get_basename())
            fname = self.get_target_filename(d)
            fname = './' + fname # Hack to make ldd find the library.
            args.append(fname)
        return args

    def generate_configure_files(self):
        for cf in self.build.get_configure_files():
            infile = os.path.join(self.environment.get_source_dir(),
                                  cf.get_subdir(),
                                  cf.get_source_name())
            outdir = os.path.join(self.environment.get_build_dir(),
                                   cf.get_subdir())
            os.makedirs(outdir, exist_ok=True)
            outfile = os.path.join(outdir, cf.get_target_name())
            do_conf_file(infile, outfile, self.interpreter.get_variables())

class NinjaBackend(Backend):

    def __init__(self, build, interp):
        Backend.__init__(self, build, interp)
        self.ninja_filename = 'build.ninja'

    def generate(self):
        outfilename = os.path.join(self.environment.get_build_dir(), self.ninja_filename)
        outfile = open(outfilename, 'w')
        self.generate_configure_files()
        outfile.write('# This is the build file for project "%s"\n' % self.build.get_project())
        outfile.write('# It is autogenerated. Do not edit by hand.\n\n')
        self.generate_rules(outfile)
        outfile.write('# Build rules for targets\n\n')
        [self.generate_target(t, outfile) for t in self.build.get_targets().values()]
        outfile.write('# Test rules\n\n')
        self.generate_tests(outfile)
        outfile.write('# Install rules\n\n')
        self.generate_install(outfile)
        if self.environment.new_coredata.coverage:
            outfile.write('# Coverage rules\n\n')
            self.generate_coverage_rules(outfile)
        outfile.write('# Suffix\n\n')
        self.generate_ending(outfile)

    def generate_coverage_rules(self, outfile):
        (gcovr_exe, lcov_exe, genhtml_exe) = environment.find_coverage_tools()
        added_rule = False
        if gcovr_exe:
            added_rule = True
            xmlbuild = 'build coverage-xml: CUSTOM_COMMAND\n'
            xmlcommand = " COMMAND = '%s' -x -r '%s' -o coverage.xml\n\n" %\
                (ninja_quote(gcovr_exe), ninja_quote(self.environment.get_build_dir()))
            outfile.write(xmlbuild)
            outfile.write(xmlcommand)
            textbuild = 'build coverage-text: CUSTOM_COMMAND\n'
            textcommand = " COMMAND = '%s' -r '%s' -o coverage.txt\n\n" %\
                (ninja_quote(gcovr_exe), ninja_quote(self.environment.get_build_dir()))
            outfile.write(textbuild)
            outfile.write(textcommand)
        if lcov_exe and genhtml_exe:
            added_rule = True
            phony = 'build coverage-html: phony coveragereport/index.html\n'
            htmlbuild = 'build coveragereport/index.html: CUSTOM_COMMAND\n'
            lcov_command = "'%s' --directory '%s' --capture --output-file coverage.info --no-checksum" %\
                (ninja_quote(lcov_exe), ninja_quote(self.environment.get_build_dir()))
            genhtml_command = "'%s' --prefix='%s' --output-directory coveragereport --title='Code coverage' --legend --show-details coverage.info" %\
                (ninja_quote(genhtml_exe), ninja_quote(self.environment.get_build_dir()))
            command = ' COMMAND = %s && %s\n\n' % (lcov_command, genhtml_command)
            outfile.write(phony)
            outfile.write(htmlbuild)
            outfile.write(command)
        if not added_rule:
            print('Warning: coverage requested but neither gcovr nor lcov/genhtml found.')

    def generate_install(self, outfile):
        script_root = self.environment.get_script_dir()
        install_script = os.path.join(script_root, 'meson_install.py')
        install_data_file = os.path.join(self.environment.get_scratch_dir(), 'install.dat')
        depfixer = os.path.join(script_root, 'depfixer.py')
        d = InstallData(self.environment.get_prefix(), depfixer, './') # Fixme

        outfile.write('build install: CUSTOM_COMMAND | all\n')
        outfile.write(" COMMAND = '%s' '%s'\n\n" % (ninja_quote(install_script), ninja_quote(install_data_file)))
        self.generate_target_install(d)
        self.generate_header_install(d)
        self.generate_man_install(d)
        self.generate_data_install(d)
        ofile = open(install_data_file, 'wb')
        pickle.dump(d, ofile)

    def generate_target_install(self, d):
        libdir = self.environment.get_libdir()
        bindir = self.environment.get_bindir()

        should_strip = self.environment.new_coredata.strip
        for t in self.build.get_targets().values():
            if t.should_install():
                if isinstance(t, interpreter.Executable):
                    outdir = bindir
                else:
                    outdir = libdir
                i = [self.get_target_filename(t), outdir, t.get_aliaslist(), should_strip]
                d.targets.append(i)

    def generate_header_install(self, d):
        incroot = self.environment.get_includedir()
        headers = self.build.get_headers()

        for h in headers:
            outdir = os.path.join(incroot, h.get_subdir())
            for f in h.get_sources():
                abspath = os.path.join(self.environment.get_source_dir(), f) # FIXME
                i = [abspath, outdir]
                d.headers.append(i)

    def generate_man_install(self, d):
        manroot = self.environment.get_mandir()
        man = self.build.get_man()
        for m in man:
            for f in m.get_sources():
                num = f.split('.')[-1]
                subdir = 'man' + num
                srcabs = os.path.join(self.environment.get_source_dir(), f)
                dstabs = os.path.join(manroot, 
                                      os.path.join(subdir, f + '.gz'))
                i = [srcabs, dstabs]
                d.man.append(i)

    def generate_data_install(self, d):
        dataroot = self.environment.get_datadir()
        data = self.build.get_data()
        for de in data:
            subdir = os.path.join(dataroot, de.get_subdir())
            for f in de.get_sources():
                srcabs = os.path.join(self.environment.get_source_dir(), f)
                dstabs = os.path.join(subdir, f)
                i = [srcabs, dstabs]
                d.data.append(i)

    def generate_tests(self, outfile):
        script_root = self.environment.get_script_dir()
        test_script = os.path.join(script_root, 'meson_test.py')
        test_data = os.path.join(self.environment.get_scratch_dir(), 'meson_test_setup.dat')
        outfile.write('build test: CUSTOM_COMMAND\n')
        outfile.write(' COMMAND = \'%s\' \'%s\'\n\n' % (ninja_quote(test_script), ninja_quote(test_data)))
        datafile = open(test_data, 'w')
        for t in self.build.get_tests():
            datafile.write(self.get_target_filename(t.get_exe()) + '\n')
        datafile.close()

    def generate_rules(self, outfile):
        outfile.write('# Rules for compiling.\n\n')
        self.generate_compile_rules(outfile)
        outfile.write('# Rules for linking.\n\n')
        self.generate_static_link_rules(outfile)
        self.generate_dynamic_link_rules(outfile)
        outfile.write('# Other rules\n\n')
        outfile.write('rule CUSTOM_COMMAND\n')
        outfile.write(' command = $COMMAND\n')
        outfile.write(' restat = 1\n\n')
        outfile.write('rule REGENERATE_BUILD\n')
        c = (ninja_quote(self.environment.get_build_command()),
             ninja_quote(self.environment.get_source_dir()),
             ninja_quote(self.environment.get_build_dir()))
        outfile.write(" command = '%s' '%s' '%s' --backend ninja\n" % c)
        outfile.write(' description = Regenerating build files\n')
        outfile.write(' generator = 1\n\n')

    def generate_static_link_rules(self, outfile):
        static_linker = self.build.static_linker
        rule = 'rule STATIC_LINKER\n'
        command = ' command = %s  $LINK_FLAGS $out $in\n' % \
        ' '.join(static_linker.get_exelist())
        description = ' description = Static linking library $out\n\n'
        outfile.write(rule)
        outfile.write(command)
        outfile.write(description)

    def generate_dynamic_link_rules(self, outfile):
        for compiler in self.build.compilers:
            langname = compiler.get_language()
            rule = 'rule %s_LINKER\n' % langname
            command = ' command = %s $FLAGS  %s $out $in $LINK_FLAGS $aliasing\n' % \
            (' '.join(compiler.get_exelist()),\
             ' '.join(compiler.get_output_flags()))
            description = ' description = Linking target $out'
            outfile.write(rule)
            outfile.write(command)
            outfile.write(description)
            outfile.write('\n')
        outfile.write('\n')

    def generate_compile_rules(self, outfile):
        for compiler in self.build.compilers:
            langname = compiler.get_language()
            rule = 'rule %s_COMPILER\n' % langname
            depflags = compiler.get_dependency_gen_flags('$out', '$DEPFILE')
            command = " command = %s $FLAGS %s %s $out %s $in\n" % \
            (' '.join(compiler.get_exelist()),\
             ' '.join(['\'%s\''% d for d in depflags]),\
             ' '.join(compiler.get_output_flags()),\
             ' '.join(compiler.get_compile_only_flags()))
            description = ' description = Compiling %s object $out\n' % langname
            dep = ' depfile = $DEPFILE\n'
            outfile.write(rule)
            outfile.write(command)
            outfile.write(dep)
            outfile.write(description)
            outfile.write('\n')
        outfile.write('\n')

    def generate_custom_generator_rules(self, target, outfile):
        for genlist in target.get_generated_sources():
            generator = genlist.get_generator()
            exe = generator.get_exe()
            infilelist = genlist.get_infilelist()
            outfilelist = genlist.get_outfilelist()
            if len(infilelist) != len(outfilelist):
                raise RuntimeError('Internal data structures broken.')
            exe_file = os.path.join(self.environment.get_build_dir(), self.get_target_filename(exe))
            base_args = generator.get_arglist()
            for i in range(len(infilelist)):
                infilename = os.path.join(self.build_to_src, infilelist[i])
                outfilename = os.path.join(self.get_target_dir(target), outfilelist[i])
                args = [x.replace("@INPUT@", infilename).replace('@OUTPUT@', outfilename)\
                        for x in base_args]
                cmdlist = [exe_file] + args
                build = 'build %s: CUSTOM_COMMAND %s | %s\n' % \
                (ninja_quote(outfilename), ninja_quote(infilename), ninja_quote(self.get_target_filename(exe)))
                command = ' COMMAND = %s\n\n' % \
                ' '.join(["'%s'" % ninja_quote(i) for i in cmdlist])
                outfile.write(build)
                outfile.write(command)

    def generate_single_compile(self, target, outfile, src, is_generated=False):
        compiler = self.get_compiler_for_source(src)
        commands = self.generate_basic_compiler_flags(target, compiler)
        if is_generated:
            abs_src = src
        else:
            abs_src = os.path.join(self.build_to_src, target.get_source_subdir(), src)
        abs_obj = os.path.join(self.get_target_private_dir(target), src)
        abs_obj += '.' + self.environment.get_object_suffix()
        dep_file = abs_obj + '.' + compiler.get_depfile_suffix()
        pchlist = target.get_pch()
        if len(pchlist) == 0:
            pch_dep = ''
        else:
            arr = []
            for pch in pchlist:
                i = os.path.join(self.get_target_private_dir(target),
                                  os.path.split(pch)[-1] + '.' + compiler.get_pch_suffix())
                arr.append(i)
            pch_dep = '|| ' + ' '.join([ninja_quote(i) for i in arr])
        for i in target.get_include_dirs():
            basedir = i.get_curdir()
            for d in i.get_incdirs():
                expdir =  os.path.join(basedir, d)
                fulldir = os.path.join(self.environment.get_source_dir(), expdir)
                barg = compiler.get_include_arg(expdir)
                sarg = compiler.get_include_arg(fulldir)
                commands.append(barg)
                commands.append(sarg)
        commands += self.get_pch_include_args(compiler, target)
        compiler_name = '%s_COMPILER' % compiler.get_language()
        build = 'build %s: %s %s %s\n' % \
        (ninja_quote(abs_obj), compiler_name, ninja_quote(abs_src),
         pch_dep)
        dep = ' DEPFILE = %s\n' % dep_file
        flags = ' FLAGS = %s\n\n' % ' '.join(["'" + ninja_quote(t) + "'" for t in commands])
        outfile.write(build)
        outfile.write(dep)
        outfile.write(flags)
        return abs_obj

    def generate_pch(self, target, outfile):
        for pch in target.get_pch():
            if '/' not in pch:
                raise interpreter.InvalidArguments('Precompiled header of "%s" must not be in the same direcotory as source, please put it in a subdirectory.' % target.get_basename())
            compiler = self.get_compiler_for_source(pch)
            commands = []
            commands += self.generate_basic_compiler_flags(target, compiler)
            src = os.path.join(self.build_to_src, target.get_source_subdir(), pch)
            dst = os.path.join(self.get_target_private_dir(target),
                                  os.path.split(pch)[-1] + '.' + compiler.get_pch_suffix())
            dep = dst + '.' + compiler.get_depfile_suffix()
            build = 'build %s: %s %s\n' % (ninja_quote(dst), 
                                           ninja_quote(compiler.get_language() + '_COMPILER'),
                                           ninja_quote(src))
            flags = ' FLAGS = %s\n' % ' '.join([ninja_quote(t) for t in commands])
            depfile = ' DEPFILE = %s\n\n' % ninja_quote(dep)
            outfile.write(build)
            outfile.write(flags)
            outfile.write(depfile)

    def generate_link(self, target, outfile, outname, obj_list):
        if isinstance(target, interpreter.StaticLibrary):
            linker = self.build.static_linker
            linker_base = 'STATIC'
        else:
            linker = self.build.compilers[0]
            linker_base = linker.get_language() # Fixme.
        linker_rule = linker_base + '_LINKER'
        commands = []
        if isinstance(target, interpreter.Executable):
            commands += linker.get_std_exe_link_flags()
        elif isinstance(target, interpreter.SharedLibrary):
            commands += linker.get_std_shared_lib_link_flags()
            commands += linker.get_pic_flags()
        elif isinstance(target, interpreter.StaticLibrary):
            commands += linker.get_std_link_flags()
        else:
            raise RuntimeError('Unknown build target type.')
        for dep in target.get_external_deps():
            commands += dep.get_link_flags()
        dependencies = target.get_dependencies()
        commands += self.build_target_link_arguments(dependencies)
        if self.environment.new_coredata.coverage:
            commands += linker.get_coverage_link_flags()
        if len(dependencies) == 0:
            dep_targets = ''
        else:
            dep_targets = '| ' + ' '.join([ninja_quote(self.get_target_filename(t)) for t in dependencies])
        build = 'build %s: %s %s %s\n' % \
        (ninja_quote(outname), linker_rule, ' '.join([ninja_quote(i) for i in obj_list]),
         dep_targets)
        flags = ' LINK_FLAGS = %s\n' % ' '.join([ninja_quote(a) for a in commands])
        outfile.write(build)
        outfile.write(flags)

    def generate_shlib_aliases(self, target, outdir, outfile):
        basename = target.get_filename()
        aliases = target.get_aliaslist()
        aliascmd = ''
        for alias in aliases:
            aliasfile = os.path.join(outdir, alias)
            cmd = " && ln -s -f '%s' '%s'" % (ninja_quote(basename), ninja_quote(aliasfile))
            aliascmd += cmd
        outfile.write(' aliasing =%s\n\n' % aliascmd)

    def generate_ending(self, outfile):
        targetlist = [self.get_target_filename(t) for t in self.build.get_targets().values()]
        build = 'build all: phony %s\n' % ' '.join(targetlist)
        default = 'default all\n\n'
        outfile.write(build)
        outfile.write(default)

        deps = [ ninja_quote(os.path.join(self.build_to_src, df)) \
                for df in self.interpreter.get_build_def_files()]
        depstr = ' '.join(deps)
        buildregen = 'build build.ninja: REGENERATE_BUILD | %s\n\n' % depstr
        outfile.write(buildregen)

        ignore_missing = 'build %s: phony\n\n' % depstr
        outfile.write(ignore_missing)

class ShellBackend(Backend):
    def __init__(self, build, interp):
        Backend.__init__(self, build, interp)
        self.build_filename = 'compile.sh'
        self.test_filename = 'run_tests.sh'
        self.install_filename = 'install.sh'

    def generate(self):
        self.generate_compile_script()
        self.generate_test_script()
        self.generate_install_script()

    def create_shfile(self, outfilename, message):
        outfile = open(outfilename, 'w')
        outfile.write('#!/bin/sh\n\n')
        outfile.write(message)
        cdcmd = ['cd', self.environment.get_build_dir()]
        outfile.write(' '.join(shell_quote(cdcmd)) + '\n')
        os.chmod(outfilename, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC |\
                 stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        return outfile

    def generate_compile_script(self):
        outfilename = os.path.join(self.environment.get_build_dir(), self.build_filename)
        message = """echo This is an autogenerated shell script build file for project \\"%s\\"
echo This is experimental and most likely will not work!
""" % self.build.get_project()
        outfile = self.create_shfile(outfilename, message)
        self.generate_commands(outfile)
        outfile.close()

    def generate_test_script(self):
        outfilename = os.path.join(self.environment.get_build_dir(), self.test_filename)
        message = """echo This is an autogenerated test script for project \\"%s\\"
echo This is experimental and most likely will not work!
echo Run compile.sh before this or bad things will happen.
""" % self.build.get_project()
        outfile = self.create_shfile(outfilename, message)
        self.generate_tests(outfile)
        outfile.close()

    def generate_install_script(self):
        outfilename = os.path.join(self.environment.get_build_dir(), self.install_filename)
        message = """echo This is an autogenerated install script for project \\"%s\\"
echo This is experimental and most likely will not work!
echo Run compile.sh before this or bad things will happen.
""" % self.build.get_project()
        outfile = self.create_shfile(outfilename, message)
        self.generate_configure_files()
        self.generate_target_install(outfile)
        self.generate_header_install(outfile)
        self.generate_man_install(outfile)
        self.generate_data_install(outfile)
        outfile.close()
    
    def make_subdir(self, outfile, sdir):
        cmdlist = ['mkdir', '-p', sdir]
        outfile.write(' '.join(shell_quote(cmdlist)) + ' || exit\n')
        
    def copy_file(self, outfile, filename, outdir):
        cpcommand = ['cp', filename, outdir]
        cpcommand = ' '.join(shell_quote(cpcommand)) + ' || exit\n'
        outfile.write(cpcommand)

    def generate_data_install(self, outfile):
        prefix = self.environment.get_prefix()
        dataroot = os.path.join(prefix, self.environment.get_datadir())
        data = self.build.get_data()
        if len(data) != 0:
            outfile.write('\necho Installing data files.\n')
        else:
            outfile.write('\necho This project has no data files to install.\n')
        for d in data:
            subdir = os.path.join(dataroot, d.get_subdir())
            absdir = os.path.join(self.environment.get_prefix(), subdir)
            for f in d.get_sources():
                self.make_subdir(outfile, absdir)
                srcabs = os.path.join(self.environment.get_source_dir(), f)
                dstabs = os.path.join(absdir, f)
                self.copy_file(outfile, srcabs, dstabs)

    def generate_man_install(self, outfile):
        prefix = self.environment.get_prefix()
        manroot = os.path.join(prefix, self.environment.get_mandir())
        man = self.build.get_man()
        if len(man) != 0:
            outfile.write('\necho Installing man pages.\n')
        else:
            outfile.write('\necho This project has no man pages to install.\n')
        for m in man:
            for f in m.get_sources():
                num = f.split('.')[-1]
                subdir = 'man' + num
                absdir = os.path.join(manroot, subdir)
                self.make_subdir(outfile, absdir)
                srcabs = os.path.join(self.environment.get_source_dir(), f)
                dstabs = os.path.join(manroot, 
                                      os.path.join(subdir, f + '.gz'))
                cmd = "gzip < '%s' > '%s' || exit\n" % (srcabs, dstabs)
                outfile.write(cmd)

    def generate_header_install(self, outfile):
        prefix = self.environment.get_prefix()
        incroot = os.path.join(prefix, self.environment.get_includedir())
        headers = self.build.get_headers()
        if len(headers) != 0:
            outfile.write('\necho Installing headers.\n')
        else:
            outfile.write('\necho This project has no headers to install.\n')
        for h in headers:
            outdir = os.path.join(incroot, h.get_subdir())
            self.make_subdir(outfile, outdir)
            for f in h.get_sources():
                abspath = os.path.join(self.environment.get_source_dir(), f) # FIXME
                self.copy_file(outfile, abspath, outdir)

    def generate_target_install(self, outfile):
        prefix = self.environment.get_prefix()
        libdir = os.path.join(prefix, self.environment.get_libdir())
        bindir = os.path.join(prefix, self.environment.get_bindir())
        self.make_subdir(outfile, libdir)
        self.make_subdir(outfile, bindir)
        if len(self.build.get_targets()) != 0:
            outfile.write('\necho Installing targets.\n')
        else:
            outfile.write('\necho This project has no targets to install.\n')
        for tmp in self.build.get_targets().items():
            (name, t) = tmp
            if t.should_install():
                if isinstance(t, interpreter.Executable):
                    outdir = bindir
                else:
                    outdir = libdir
                outfile.write('echo Installing "%s".\n' % name)
                self.copy_file(outfile, self.get_target_filename(t), outdir)
                self.generate_shlib_aliases(t, outdir, outfile)
                self.fix_deps(outfile, t, outdir)

    def fix_deps(self, outfile, target, outdir):
        if isinstance(target, interpreter.StaticLibrary):
            return
        depfixer = self.environment.get_depfixer()
        fname = os.path.join(outdir, target.get_filename())
        cmds = [depfixer, fname, './']
        outfile.write(' '.join(shell_quote(cmds)) + ' || exit\n')

    def generate_tests(self, outfile):
        for t in self.build.get_tests():
            cmds = []
            cmds.append(self.get_target_filename(t.get_exe()))
            outfile.write('echo Running test \\"%s\\".\n' % t.get_name())
            outfile.write(' '.join(shell_quote(cmds)) + ' || exit\n')


    def generate_single_compile(self, target, outfile, src):
        compiler = self.get_compiler_for_source(src)
        commands = []
        commands += compiler.get_exelist()
        commands += self.generate_basic_compiler_flags(target, compiler)
        commands += compiler.get_compile_only_flags()
        abs_src = os.path.join(self.environment.get_source_dir(), target.get_source_subdir(), src)
        abs_obj = os.path.join(self.get_target_private_dir(target), src)
        abs_obj += '.' + self.environment.get_object_suffix()
        for i in target.get_include_dirs():
            basedir = i.get_curdir()
            for d in i.get_incdirs():
                expdir =  os.path.join(basedir, d)
                fulldir = os.path.join(self.environment.get_source_dir(), expdir)
                barg = compiler.get_include_arg(expdir)
                sarg = compiler.get_include_arg(fulldir)
                commands.append(barg)
                commands.append(sarg)
        commands += self.get_pch_include_args(compiler, target)
        commands.append(abs_src)
        commands += compiler.get_output_flags()
        commands.append(abs_obj)
        quoted = shell_quote(commands)
        outfile.write('\necho Compiling \\"%s\\"\n' % src)
        outfile.write(' '.join(quoted) + ' || exit\n')
        return abs_obj

    def generate_link(self, target, outfile, outname, obj_list):
        if isinstance(target, interpreter.StaticLibrary):
            linker = self.build.static_linker
        else:
            linker = self.build.compilers[0] # Fixme.
        commands = []
        commands += linker.get_exelist()
        if isinstance(target, interpreter.StaticLibrary):
            commands += linker.get_std_link_flags()
        commands += linker.get_output_flags()
        commands.append(outname)
        commands += obj_list
        if isinstance(target, interpreter.Executable):
            commands += linker.get_std_exe_link_flags()
        elif isinstance(target, interpreter.SharedLibrary):
            commands += linker.get_std_shared_lib_link_flags()
            commands += linker.get_pic_flags()
        elif isinstance(target, interpreter.StaticLibrary):
            pass
        else:
            raise RuntimeError('Unknown build target type.')
        for dep in target.get_external_deps():
            commands += dep.get_link_flags()
        commands += self.build_target_link_arguments(target.get_dependencies())
        quoted = shell_quote(commands)
        outfile.write('\necho Linking \\"%s\\".\n' % target.get_basename())
        outfile.write(' '.join(quoted) + ' || exit\n')

    def generate_commands(self, outfile):
        for i in self.build.get_targets().items():
            target = i[1]
            self.generate_target(target, outfile)


    def generate_pch(self, target, outfile):
        print('Generating pch for "%s"' % target.get_basename())
        for pch in target.get_pch():
            if '/' not in pch:
                raise interpreter.InvalidArguments('Precompiled header of "%s" must not be in the same direcotory as source, please put it in a subdirectory.' % target.get_basename())
            compiler = self.get_compiler_for_source(pch)
            commands = []
            commands += compiler.get_exelist()
            commands += self.generate_basic_compiler_flags(target, compiler)
            srcabs = os.path.join(self.environment.get_source_dir(), target.get_source_subdir(), pch)
            dstabs = os.path.join(self.environment.get_build_dir(),
                                   self.get_target_private_dir(target),
                                   os.path.split(pch)[-1] + '.' + compiler.get_pch_suffix())
            commands.append(srcabs)
            commands += compiler.get_output_flags()
            commands.append(dstabs)
            quoted = shell_quote(commands)
            outfile.write('\necho Generating pch \\"%s\\".\n' % pch)
            outfile.write(' '.join(quoted) + ' || exit\n')

    def generate_shlib_aliases(self, target, outdir, outfile):
        basename = target.get_filename()
        aliases = target.get_aliaslist()
        for alias in aliases:
            aliasfile = os.path.join(outdir, alias)
            cmd = ['ln', '-s', '-f', basename, aliasfile]
            outfile.write(' '.join(shell_quote(cmd)) + '|| exit\n')
