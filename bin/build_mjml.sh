#!/bin/sh

command_exists () {
    type "$1" &> /dev/null ;
}

USAGE=$(cat <<-END
Emails build script. How to use:
  mjml <directories> [-m|--minify]

  Arguments:
    directories --  directiories for mjml files;
                    all html files will be stored in their directories
    -m          -- specifies, whether templates should be minified
END
)

MJML_REGEX="^[^_].+\.mjml$"

if [ "$1" = 'help' -o "$1" = 'h' -o "$1" = '--help' ]; then
  echo "$USAGE\n"
fi

if ! command_exists mjml; then
  echo 'mjml is not installed. Please run `npm i -g mjml` to proceed\n'
  exit 1
fi

for arg in $@; do
  if [ "$arg" = '-m' -o "$arg" = '--minify' ]; then
    minify='-m'
  else
    if [ -z $dirs ]
    then
      dirs=$arg
    else
      dirs="$dirs $arg"
    fi
  fi
done

if [ -z $dirs ]; then 
  echo -n 'Please, specify directory, where the files to build are!\n'
  exit 2
fi

for dirname in $dirs; do
  if [ -d $dirname ]; then
    echo -n "Building for directory $dirname "
    for file in $(ls $dirname | grep -E $MJML_REGEX )
    do
      filename_mjml=$(basename "$file")
      filename="${filename_mjml%.*}"
      from_file="$dirname/$filename_mjml"
      to_file="$dirname/$filename.html"
      mjml -r $from_file -o $to_file ${minify}
      if [ $? -eq 0 ]; then
        echo -n '.'
      else
        echo -n 'ERROR\nAn error occured, please check it out\n'
        exit 3
      fi
    done
    echo -n ' Done\n'
  else 
    echo -n "\nDirectory '$dirname' was not found: skipping\n"
  fi
done

